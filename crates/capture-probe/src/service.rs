//! Supervised resident RGB-D worker. A separate process bounds native hangs.
use crate::rgbd::{self, Config, Metrics, QueuedPair};
use anyhow::{Context, Result, ensure};
use frame_core::{LatestQueue, PairGuard, PairProvenance};
use ring::digest;
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            fs::{OpenOptionsExt, PermissionsExt},
            process::CommandExt,
        },
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn stop_signal(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}
fn install_signals() -> Result<()> {
    // The handler performs only an atomic store; all SDK shutdown runs on worker thread.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = stop_signal as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        ensure!(
            libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut()) == 0
                && libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut()) == 0,
            "signal setup failed"
        );
    }
    Ok(())
}
struct Settings {
    status_path: PathBuf,
    generation: String,
    idle_timeout_ms: u64,
    startup_timeout_ms: u64,
    heartbeat_ms: u64,
    heartbeat_timeout_ms: u64,
    max_restarts: u64,
    restart_backoff_ms: u64,
    shutdown_timeout_ms: u64,
    ready_min_pairs: u64,
}
struct ServiceConfig {
    capture: Config,
    settings: Settings,
    hash: String,
    runtime_contract: Option<Value>,
}
fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        result.push(DIGITS[(byte >> 4) as usize] as char);
        result.push(DIGITS[(byte & 15) as usize] as char);
    }
    result
}
fn sha(bytes: &[u8]) -> String {
    hex(digest::digest(&digest::SHA256, bytes).as_ref())
}
impl ServiceConfig {
    fn parse(raw: &str) -> Result<Self> {
        ensure!(raw.len() <= 64 * 1024, "config too large");
        let mut value: Value = serde_json::from_str(raw)?;
        let runtime_contract = value
            .as_object_mut()
            .context("config object required")?
            .remove("runtime_contract");
        if let Some(contract) = &runtime_contract {
            let object = contract
                .as_object()
                .context("runtime contract object required")?;
            let keys = ["sdk_sha256", "bridge_sha256", "service_sha256"];
            ensure!(
                object.len() == 3
                    && keys
                        .iter()
                        .all(
                            |key| object
                                .get(*key)
                                .and_then(Value::as_str)
                                .is_some_and(|v| v.len() == 64
                                    && v.bytes()
                                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
                        ),
                "invalid runtime contract"
            );
        }
        let service = value
            .as_object_mut()
            .context("config object required")?
            .remove("service")
            .context("explicit service policy required")?;
        let keys = [
            "status_path",
            "generation",
            "idle_timeout_ms",
            "startup_timeout_ms",
            "heartbeat_ms",
            "heartbeat_timeout_ms",
            "max_restarts",
            "restart_backoff_ms",
            "shutdown_timeout_ms",
            "ready_min_pairs",
        ];
        let object = service.as_object().context("service object required")?;
        ensure!(
            object.len() == keys.len() && keys.iter().all(|k| object.contains_key(*k)),
            "service fields required"
        );
        ensure!(
            value.get("seconds").is_none(),
            "resident service refuses finite seconds"
        );
        value["seconds"] = json!(300);
        let capture = Config::parse(&value.to_string())?;
        ensure!(
            capture.ros.is_some(),
            "resident service requires real ROS output"
        );
        let number = |key: &str| -> Result<u64> {
            service[key]
                .as_u64()
                .context("unsigned service limit required")
        };
        let status_path = PathBuf::from(
            service["status_path"]
                .as_str()
                .context("status path required")?,
        );
        ensure!(
            status_path.is_absolute()
                && status_path.file_name().is_some()
                && status_path
                    .parent()
                    .is_some_and(|p| p != Path::new("/") && p != Path::new("/tmp")),
            "private absolute status directory required"
        );
        let generation = service["generation"]
            .as_str()
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= 128
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            })
            .context("opaque generation required")?
            .to_owned();
        let settings = Settings {
            status_path,
            generation,
            idle_timeout_ms: number("idle_timeout_ms")?,
            startup_timeout_ms: number("startup_timeout_ms")?,
            heartbeat_ms: number("heartbeat_ms")?,
            heartbeat_timeout_ms: number("heartbeat_timeout_ms")?,
            max_restarts: number("max_restarts")?,
            restart_backoff_ms: number("restart_backoff_ms")?,
            shutdown_timeout_ms: number("shutdown_timeout_ms")?,
            ready_min_pairs: number("ready_min_pairs")?,
        };
        ensure!(
            (100..=60_000).contains(&settings.idle_timeout_ms)
                && settings.idle_timeout_ms >= u64::from(capture.poll_ms)
                && (1_000..=120_000).contains(&settings.startup_timeout_ms)
                && settings.startup_timeout_ms > settings.idle_timeout_ms
                && (50..=5_000).contains(&settings.heartbeat_ms)
                && (500..=120_000).contains(&settings.heartbeat_timeout_ms)
                && settings.heartbeat_timeout_ms >= settings.heartbeat_ms.saturating_mul(3)
                && (0..=20).contains(&settings.max_restarts)
                && (100..=30_000).contains(&settings.restart_backoff_ms)
                && (100..=30_000).contains(&settings.shutdown_timeout_ms)
                && (1..=10_000).contains(&settings.ready_min_pairs),
            "service policy bounds"
        );
        ensure!(
            capture.startup_discovery_seconds * 1000.0 < settings.startup_timeout_ms as f64,
            "startup allowance exceeds deadline"
        );
        Ok(Self {
            capture,
            settings,
            hash: sha(raw.as_bytes()),
            runtime_contract,
        })
    }
}
impl ServiceConfig {
    fn verify_runtime(&self) -> Result<()> {
        if let Some(contract) = &self.runtime_contract {
            for (key, path) in [
                ("sdk_sha256", PathBuf::from(&self.capture.sdk_library)),
                (
                    "bridge_sha256",
                    PathBuf::from(&self.capture.ros.as_ref().context("ROS required")?.bridge),
                ),
                ("service_sha256", std::env::current_exe()?),
            ] {
                ensure!(
                    sha(&fs::read(path)?)
                        == contract[key].as_str().context("runtime hash required")?,
                    "runtime artifact mismatch"
                );
            }
        }
        Ok(())
    }
}

fn random_session() -> Result<String> {
    let mut bytes = [0_u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(hex(&bytes))
}
fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |v| v.as_millis().min(u64::MAX as u128) as u64)
}
fn atomic_status(path: &Path, value: &Value) -> Result<()> {
    let parent = path.parent().context("status parent required")?;
    fs::create_dir_all(parent)?;
    ensure!(
        !fs::symlink_metadata(parent)?.file_type().is_symlink(),
        "status directory cannot be symlink"
    );
    fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    let temporary = parent.join(format!(
        ".status-{}-{}.tmp",
        std::process::id(),
        random_session()?
    ));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        serde_json::to_writer(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(temporary);
    result
}
struct Status<'a> {
    config: &'a ServiceConfig,
    session: String,
    heartbeat: u64,
    started: Instant,
    restarts: u64,
    last_failure: Option<&'static str>,
}
impl Status<'_> {
    fn write(
        &mut self,
        state: &str,
        reason: Option<&'static str>,
        metrics: &Metrics,
        last_pair: Option<Instant>,
    ) -> Result<()> {
        self.heartbeat += 1;
        if reason.is_some() && reason != Some("SESSION_FAILED") {
            self.last_failure = reason;
        }
        atomic_status(
            &self.config.settings.status_path,
            &json!({"version":1,"kind":"capture-service-status","state":state,"ready":state=="ready","reason":reason,"last_failure_reason":self.last_failure,"session_id":self.session,"generation":self.config.settings.generation,"config_sha256":self.config.hash,"pid":std::process::id(),"heartbeat_sequence":self.heartbeat,"updated_unix_ms":unix_ms(),"uptime_ms":self.started.elapsed().as_millis(),"restart_attempt":self.restarts,"last_pair_age_ms":last_pair.map(|v|v.elapsed().as_millis()),"pairs_accepted":metrics.accepted,"pairs_rejected":metrics.rejected,"pairs_published":metrics.published_pairs,"individual_publish_calls":metrics.publish_calls,"publish_failures":metrics.publish_failures,"stamp_failures":metrics.stamp_failures,"framesets_observed":metrics.framesets,"missing_pairs":metrics.missing,"skew_rejections":metrics.skew,"monotonic_rejections":metrics.monotonic,"poll_timeouts":metrics.timeouts,"physical_sync_verified":false,"delivery_verified":false}),
        )
    }
}
fn arm_parent_death(expected_parent: u32) -> Result<()> {
    // Docker ENTRYPOINT supervisors legitimately own PID 1 in their namespace.
    ensure!(expected_parent >= 1, "supervisor PID required");
    #[cfg(target_os = "linux")]
    unsafe {
        // Register before any config/SDK work. Checking the caller-supplied PID
        // after registration closes the parent-died-before-prctl race.
        ensure!(
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL, 0, 0, 0) == 0,
            "parent death signal setup failed"
        );
        ensure!(
            libc::getppid() == expected_parent as libc::pid_t,
            "supervisor already exited"
        );
    }
    Ok(())
}
fn worker(config: &ServiceConfig, restart: u64) -> Result<()> {
    let mut status = Status {
        config,
        session: random_session()?,
        heartbeat: 0,
        started: Instant::now(),
        restarts: restart,
        last_failure: None,
    };
    let mut metrics = Metrics::default();
    let mut last_pair = None;
    status.write("starting", None, &metrics, last_pair)?;
    let outcome = (|| -> Result<()> {
        ensure!(
            cfg!(target_os = "linux") && cfg!(target_endian = "little"),
            "supported SDK host required"
        );
        config.verify_runtime()?;
        let sdk = orbbec_safe::Sdk::load(&config.capture.sdk_library)?;
        let mut pipeline = sdk.open(
            &config.capture.serial,
            Some(config.capture.color.profile),
            Some(config.capture.depth.profile),
        )?;
        let mut ros = None;
        let mut queue = LatestQueue::new(config.capture.capacity)?;
        let mut guard = PairGuard::default();
        let opened = Instant::now();
        let warmup = Duration::from_secs_f64(config.capture.startup_discovery_seconds);
        let mut heartbeat = Instant::now();
        let mut consecutive = 0_u64;
        let mut had_ready = false;
        let mut sequence = 0_u64;
        let result = (|| -> Result<()> {
            while !STOP.load(Ordering::Relaxed) {
                let startup = opened.elapsed() < warmup;
                let mut state = if had_ready && consecutive >= config.settings.ready_min_pairs {
                    "ready"
                } else if had_ready {
                    "degraded"
                } else {
                    "starting"
                };
                if heartbeat.elapsed() >= Duration::from_millis(config.settings.heartbeat_ms) {
                    status.write(state, None, &metrics, last_pair)?;
                    heartbeat = Instant::now();
                }
                ensure!(
                    had_ready
                        || status.started.elapsed()
                            < Duration::from_millis(config.settings.startup_timeout_ms),
                    "startup deadline"
                );
                if !startup
                    && last_pair.is_some_and(|v: Instant| {
                        v.elapsed() > Duration::from_millis(config.settings.idle_timeout_ms)
                    })
                {
                    status.write("degraded", Some("STREAM_TIMEOUT"), &metrics, last_pair)?;
                    anyhow::bail!("stream timeout");
                }
                let Some(set) = pipeline.wait(config.capture.poll_ms)? else {
                    metrics.timeouts += 1;
                    continue;
                };
                sequence += 1;
                metrics.framesets += 1;
                let received = Instant::now();
                let color = set.color()?;
                let depth = set.depth()?;
                if color.is_none() || depth.is_none() {
                    metrics.missing += 1;
                    metrics.reject("MISSING_FRAME");
                    consecutive = 0;
                    if !startup {
                        status.write(
                            "degraded",
                            Some("PAIR_CONTRACT_REJECTED"),
                            &metrics,
                            last_pair,
                        )?;
                    }
                    continue;
                }
                if ros.is_none() {
                    ros = rgbd::setup_ros(&config.capture, &set.calibration()?)?;
                    if let Some(output) = &mut ros {
                        output.color.set_session_id(&status.session)?;
                        output.depth.set_session_id(&status.session)?;
                        output.aux.set_session_id(&status.session)?;
                    }
                }
                let packets = (|| -> Result<_> {
                    Ok((
                        rgbd::packet(
                            color.unwrap(),
                            &config.capture.color,
                            false,
                            false,
                            received,
                            &mut metrics,
                        )?,
                        rgbd::packet(
                            depth.unwrap(),
                            &config.capture.depth,
                            true,
                            config.capture.normalize,
                            received,
                            &mut metrics,
                        )?,
                    ))
                })();
                let (color, depth) = match packets {
                    Ok(value) => value,
                    Err(_) => {
                        metrics.reject("FRAME_CONTRACT_REJECTED");
                        consecutive = 0;
                        if !startup {
                            status.write(
                                "degraded",
                                Some("FRAME_CONTRACT_REJECTED"),
                                &metrics,
                                last_pair,
                            )?;
                        }
                        continue;
                    }
                };
                let lengths = (color.payload.data()?.len(), depth.payload.data()?.len());
                let pair = match guard.accept(
                    sequence,
                    Some(color),
                    Some(depth),
                    lengths,
                    config.capture.policy,
                    PairProvenance::CallerObservedSdkFrameset,
                ) {
                    Ok(value) => value,
                    Err(error) => {
                        if matches!(error, frame_core::PairError::SkewExceeded) {
                            metrics.skew += 1;
                        } else {
                            metrics.monotonic += 1;
                        }
                        metrics.reject("PAIR_CONTRACT_REJECTED");
                        consecutive = 0;
                        if !startup {
                            status.write(
                                "degraded",
                                Some("PAIR_CONTRACT_REJECTED"),
                                &metrics,
                                last_pair,
                            )?;
                        }
                        continue;
                    }
                };
                if startup {
                    continue;
                }
                metrics.accepted += 1;
                queue.push(QueuedPair {
                    pair,
                    enqueued_at: Instant::now(),
                });
                rgbd::drain(&mut queue, &mut ros, &mut metrics)?;
                last_pair = Some(Instant::now());
                consecutive += 1;
                if consecutive >= config.settings.ready_min_pairs {
                    state = "ready";
                    had_ready = true;
                }
                if state == "ready" && consecutive == config.settings.ready_min_pairs {
                    status.write(state, None, &metrics, last_pair)?;
                    heartbeat = Instant::now();
                }
            }
            Ok(())
        })();
        while queue.pop().is_some() {}
        // This native stop can hang; the parent supervisor enforces shutdown deadline.
        let stopped = pipeline.stop();
        result?;
        stopped?;
        Ok(())
    })();
    if STOP.load(Ordering::Relaxed) && outcome.is_ok() {
        status.write("stopped", Some("SIGNAL"), &metrics, last_pair)?;
        Ok(())
    } else {
        status.write("degraded", Some("SESSION_FAILED"), &metrics, last_pair)?;
        outcome.and_then(|_| anyhow::bail!("unexpected session end"))
    }
}
fn signal_child(child: &Child, signal: libc::c_int) {
    unsafe {
        libc::kill(-(child.id() as i32), signal);
    }
}
fn terminate(child: &mut Child, timeout: Duration) -> Result<()> {
    signal_child(child, libc::SIGTERM);
    let started = Instant::now();
    while child.try_wait()?.is_none() && started.elapsed() < timeout {
        thread::sleep(Duration::from_millis(25));
    }
    if child.try_wait()?.is_none() {
        signal_child(child, libc::SIGKILL);
        child.wait()?;
    }
    Ok(())
}
fn wait_interruptible(duration: Duration) {
    let start = Instant::now();
    while start.elapsed() < duration && !STOP.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_millis(25));
    }
}
fn supervisor_status(
    config: &ServiceConfig,
    state: &str,
    reason: &str,
    restarts: u64,
) -> Result<()> {
    let mut value = fs::read_to_string(&config.settings.status_path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({"version":1,"kind":"capture-service-status"}));
    if value["reason"]
        .as_str()
        .is_some_and(|v| v != "WORKER_EXITED" && v != "SESSION_FAILED")
    {
        value["worker_reason"] = value["reason"].clone();
    }
    value["ready"] = false.into();
    value["state"] = state.into();
    value["reason"] = reason.into();
    value["supervisor_pid"] = std::process::id().into();
    value["restart_attempt"] = restarts.into();
    value["updated_unix_ms"] = unix_ms().into();
    atomic_status(&config.settings.status_path, &value)
}
fn supervisor(config: &ServiceConfig, path: &Path) -> Result<()> {
    let parent = config
        .settings
        .status_path
        .parent()
        .context("status parent")?;
    fs::create_dir_all(parent)?;
    let lock = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(parent.join("service.lock"))?;
    ensure!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0,
        "service already running"
    );
    let mut restarts = 0_u64;
    loop {
        ensure!(
            sha(&fs::read(path)?) == config.hash,
            "immutable config changed"
        );
        if STOP.load(Ordering::Relaxed) {
            supervisor_status(config, "stopped", "SIGNAL", restarts)?;
            return Ok(());
        }
        let mut child = Command::new(std::env::current_exe()?)
            .args(["--worker", "--config"])
            .arg(path)
            .args([
                "--expected-config-sha",
                &config.hash,
                "--restart-attempt",
                &restarts.to_string(),
                "--supervisor-pid",
                &std::process::id().to_string(),
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()?;
        let mut last_change = Instant::now();
        let mut marker = None;
        let reason = loop {
            if STOP.load(Ordering::Relaxed) {
                terminate(
                    &mut child,
                    Duration::from_millis(config.settings.shutdown_timeout_ms),
                )?;
                break "SIGNAL";
            }
            if let Some(exit) = child.try_wait()? {
                break if exit.success() {
                    "WORKER_STOPPED"
                } else {
                    "WORKER_EXITED"
                };
            }
            if let Ok(raw) = fs::read_to_string(&config.settings.status_path) {
                if let Ok(value) = serde_json::from_str::<Value>(&raw) {
                    if value["pid"].as_u64() == Some(u64::from(child.id())) {
                        let next = (
                            value["session_id"].as_str().unwrap_or("").to_owned(),
                            value["heartbeat_sequence"].as_u64().unwrap_or(0),
                        );
                        if marker.as_ref() != Some(&next) {
                            marker = Some(next);
                            last_change = Instant::now();
                        }
                    }
                }
            }
            if last_change.elapsed() > Duration::from_millis(config.settings.heartbeat_timeout_ms) {
                terminate(
                    &mut child,
                    Duration::from_millis(config.settings.shutdown_timeout_ms),
                )?;
                break "NATIVE_HEARTBEAT_TIMEOUT";
            }
            thread::sleep(Duration::from_millis(50));
        };
        supervisor_status(
            config,
            if STOP.load(Ordering::Relaxed) {
                "stopped"
            } else {
                "degraded"
            },
            reason,
            restarts,
        )?;
        if STOP.load(Ordering::Relaxed) {
            // A signal can arrive after the degraded snapshot above was written.
            // A successful shutdown must commit its final state before returning.
            supervisor_status(config, "stopped", "SIGNAL", restarts)?;
            return Ok(());
        }
        ensure!(
            restarts < config.settings.max_restarts,
            "restart budget exhausted"
        );
        restarts += 1;
        wait_interruptible(Duration::from_millis(
            config
                .settings
                .restart_backoff_ms
                .saturating_mul(restarts.min(10)),
        ));
    }
}
pub fn main_entry() {
    let result = (|| -> Result<()> {
        install_signals()?;
        let args: Vec<_> = std::env::args().skip(1).collect();
        if args.first().is_some_and(|s| s == "--validate-config") {
            ensure!(
                args.len() == 2 && Path::new(&args[1]).is_absolute(),
                "absolute config path required"
            );
            let config = ServiceConfig::parse(&fs::read_to_string(&args[1])?)?;
            config.verify_runtime()?;
            println!(
                "{}",
                json!({"version":1,"kind":"capture-service-config-validation","valid":true,"runtime_contract_verified":config.runtime_contract.is_some(),"hardware_verified":false})
            );
            return Ok(());
        }
        let worker_mode = args.first().is_some_and(|s| s == "--worker");
        if worker_mode {
            ensure!(
                args.len() == 9 && args[7] == "--supervisor-pid",
                "invalid worker parent binding"
            );
            arm_parent_death(args[8].parse()?)?;
        }
        let offset = usize::from(worker_mode);
        ensure!(
            args.len() >= offset + 2 && args[offset] == "--config",
            "config required"
        );
        let path = PathBuf::from(&args[offset + 1]);
        ensure!(path.is_absolute(), "absolute private config required");
        let config = ServiceConfig::parse(&fs::read_to_string(&path)?)?;
        if worker_mode {
            ensure!(
                args.len() == 9
                    && args[3] == "--expected-config-sha"
                    && args[4] == config.hash
                    && args[5] == "--restart-attempt",
                "invalid worker invocation"
            );
            worker(&config, args[6].parse()?)
        } else {
            ensure!(args.len() == 2, "unexpected service argument");
            supervisor(&config, &path)
        }
    })();
    if result.is_err() {
        eprintln!(
            "{}",
            json!({"version":1,"kind":"capture-service","ok":false,"error":"CAPTURE_SERVICE_FAILED","scope":"private-diagnostics-redacted"})
        );
        std::process::exit(1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Value {
        json!({"version":1,"sdk_library":"/private/missing-sdk.so","serial":"private-device-never-output","color":{"width":640,"height":480,"fps":30,"frame_id":"color_optical"},"depth":{"width":640,"height":480,"fps":30,"frame_id":"depth_optical","unit_policy":"normalize-mm"},"pairing":{"clock":"device","max_delta_us":20000},"poll_ms":100,"capacity":4,"startup_discovery_seconds":0,"ros":{"bridge":"/private/missing-bridge.so","namespace":"/test","stamp_source":"sdk-system-unix"},"service":{"status_path":"/tmp/private-capture/status.json","generation":"test-generation","idle_timeout_ms":2000,"startup_timeout_ms":15000,"heartbeat_ms":500,"heartbeat_timeout_ms":10000,"max_restarts":3,"restart_backoff_ms":1000,"shutdown_timeout_ms":1000,"ready_min_pairs":5}})
    }
    #[test]
    fn service_config_requires_explicit_bounded_policy() {
        let mut value = config();
        assert!(ServiceConfig::parse(&value.to_string()).is_ok());
        value["service"]["max_restarts"] = json!(21);
        assert!(ServiceConfig::parse(&value.to_string()).is_err());
        value = config();
        value["seconds"] = json!(5);
        assert!(ServiceConfig::parse(&value.to_string()).is_err());
        value = config();
        value["service"]["heartbeat_timeout_ms"] = json!(500);
        assert!(ServiceConfig::parse(&value.to_string()).is_err());
    }
    #[test]
    fn runtime_contract_requires_exact_hash_fields_and_actual_bytes() {
        let mut value = config();
        value["runtime_contract"] = json!({"sdk_sha256":"a".repeat(64),"bridge_sha256":"b".repeat(64),"service_sha256":"c".repeat(64)});
        let parsed = ServiceConfig::parse(&value.to_string()).unwrap();
        assert!(parsed.verify_runtime().is_err());
        value["runtime_contract"]["unexpected"] = json!(true);
        assert!(ServiceConfig::parse(&value.to_string()).is_err());
    }
    #[test]
    fn zero_parent_pid_is_rejected() {
        assert!(arm_parent_death(0).is_err());
    }
    #[test]
    fn sessions_are_independent_128bit_lowercase_hex() {
        assert_eq!(hex(&[0, 1, 15, 16, 128, 255]), "00010f1080ff");
        assert_eq!(
            sha(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let a = random_session().unwrap();
        let b = random_session().unwrap();
        assert_eq!(a.len(), 32);
        assert!(
            a.bytes()
                .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
        );
        assert_ne!(a, b);
    }
    #[test]
    fn status_is_atomic_private_and_redacted() {
        let dir =
            std::env::temp_dir().join(format!("capture-status-test-{}", random_session().unwrap()));
        let mut value = config();
        value["service"]["status_path"] = json!(dir.join("status.json").to_str().unwrap());
        let parsed = ServiceConfig::parse(&value.to_string()).unwrap();
        let mut status = Status {
            config: &parsed,
            session: random_session().unwrap(),
            heartbeat: 0,
            started: Instant::now(),
            restarts: 0,
            last_failure: None,
        };
        status
            .write(
                "degraded",
                Some("STREAM_TIMEOUT"),
                &Metrics::default(),
                None,
            )
            .unwrap();
        status
            .write(
                "degraded",
                Some("SESSION_FAILED"),
                &Metrics::default(),
                None,
            )
            .unwrap();
        let raw = fs::read_to_string(&parsed.settings.status_path).unwrap();
        assert!(!raw.contains("private-device"));
        let actual: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(actual["heartbeat_sequence"], 2);
        assert_eq!(actual["last_failure_reason"], "STREAM_TIMEOUT");
        assert_eq!(
            fs::metadata(&parsed.settings.status_path)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn native_process_is_killed_when_graceful_deadline_expires() {
        let mut child = Command::new("sh")
            .args(["-c", "trap '' TERM; while :; do sleep 1; done"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .unwrap();
        thread::sleep(Duration::from_millis(50));
        let start = Instant::now();
        terminate(&mut child, Duration::from_millis(50)).unwrap();
        assert!(child.try_wait().unwrap().is_some());
        assert!(start.elapsed() < Duration::from_secs(2));
    }
}
