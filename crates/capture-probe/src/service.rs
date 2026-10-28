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
    recording_control_path: Option<PathBuf>,
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
    quality_runtime: Option<quality_core::RuntimeQualityPolicy>,
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
        let mut value: Value =
            serde_json::from_value(capture_contract::parse_strict(raw.as_bytes())?)?;
        let quality_runtime = value
            .as_object_mut()
            .context("config object required")?
            .remove("quality_runtime")
            .map(serde_json::from_value::<quality_core::RuntimeQualityPolicy>)
            .transpose()?;
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
            object.len() == keys.len() + usize::from(object.contains_key("recording_control_path"))
                && keys.iter().all(|k| object.contains_key(*k)),
            "service fields required"
        );
        ensure!(
            value.get("seconds").is_none(),
            "resident service refuses finite seconds"
        );
        value["seconds"] = json!(300);
        let mut capture = Config::parse(&value.to_string())?;
        if let Some(policy) = &quality_runtime {
            policy.validate().map_err(anyhow::Error::msg)?;
            ensure!(
                policy.quality.action == capture_contract::QualityAction::ReportOnly,
                "service runtime quality supports report-only"
            );
            ensure!(
                capture.quality_policy.as_ref() == Some(&policy.quality),
                "quality policies disagree"
            );
            capture.configuration["quality_runtime"] = serde_json::to_value(policy)?;
        }
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
        let recording_control_path = service
            .get("recording_control_path")
            .map(|v| -> Result<PathBuf> {
                let path = PathBuf::from(v.as_str().context("recording control path string")?);
                ensure!(
                    path.is_absolute()
                        && path.file_name().is_some_and(|p| p == "control.json")
                        && path
                            .parent()
                            .is_some_and(|p| p != Path::new("/") && p != Path::new("/tmp"))
                        && !path
                            .components()
                            .any(|c| matches!(c, std::path::Component::ParentDir)),
                    "private recording control path required"
                );
                Ok(path)
            })
            .transpose()?;
        let settings = Settings {
            status_path,
            recording_control_path,
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
            quality_runtime,
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
/// Runtime adapter owns observed receipts separately from snapshot-bound checks.
/// Before an actual SDK snapshot exists, it never invents calibration/context IDs.
struct ServiceQuality {
    engine: quality_core::QualityEngine,
    policy: quality_core::RuntimeQualityPolicy,
    session: capture_contract::SessionId,
    instance: String,
    origin: Instant,
    directory: PathBuf,
    snapshot: Option<capture_contract::snapshot::SnapshotEnvelope>,
    receipts: std::collections::BTreeMap<String, u64>,
    unbound: std::collections::BTreeMap<String, u64>,
    last_observed: std::collections::BTreeMap<String, Value>,
    finalized: bool,
    publication_calls: u64,
    publication_failures: u64,
    partial_publications: u64,
    progress: std::collections::BTreeMap<String, (u64, u64)>,
    source_progress_invalid: bool,
}
impl ServiceQuality {
    fn new(
        policy: quality_core::RuntimeQualityPolicy,
        session: &str,
        instance: &str,
        origin: Instant,
        status: &Path,
    ) -> Result<Self> {
        let session = capture_contract::SessionId::parse(session)?;
        let engine = quality_core::QualityEngine::new(
            policy.clone(),
            session.clone(),
            vec!["color".into(), "depth".into()],
        )
        .map_err(anyhow::Error::msg)?;
        Ok(Self {
            engine,
            policy,
            session: session.clone(),
            instance: instance.into(),
            origin,
            directory: status
                .parent()
                .context("status parent")?
                .join("quality")
                .join(session.as_str()),
            snapshot: None,
            receipts: std::collections::BTreeMap::from([("color".into(), 0), ("depth".into(), 0)]),
            unbound: std::collections::BTreeMap::from([("color".into(), 0), ("depth".into(), 0)]),
            last_observed: std::collections::BTreeMap::new(),
            finalized: false,
            publication_calls: 0,
            publication_failures: 0,
            partial_publications: 0,
            progress: std::collections::BTreeMap::new(),
            source_progress_invalid: false,
        })
    }
    fn at(&self, instant: Instant) -> capture_contract::MonoTime {
        capture_contract::MonoTime {
            session_id: self.session.clone(),
            offset_ns: instant
                .saturating_duration_since(self.origin)
                .as_nanos()
                .min(u64::MAX as u128) as u64,
        }
    }
    fn evaluate(
        &mut self,
        violation: quality_core::FrameViolation,
        condition: quality_core::Condition,
    ) -> Result<()> {
        self.engine
            .evaluate(
                self.at(Instant::now()),
                quality_core::Scope::Group,
                violation,
                condition,
            )
            .map_err(anyhow::Error::msg)
    }
    fn tick(&mut self) -> Result<()> {
        self.engine
            .tick(self.at(Instant::now()))
            .map_err(anyhow::Error::msg)
    }
    fn bind(&mut self, snapshot: &capture_contract::snapshot::SnapshotEnvelope) -> Result<()> {
        snapshot.require_runtime()?;
        ensure!(
            snapshot.content.binding.session_id == self.session
                && snapshot.content.binding.instance_id.as_str() == self.instance,
            "quality snapshot binding"
        );
        self.snapshot = Some(snapshot.clone());
        Ok(())
    }
    fn receipt(
        &mut self,
        stream: &str,
        metadata: Option<&orbbec_safe::FrameMetadata>,
        received: Instant,
        received_unix_ns: Option<u64>,
        contract_valid: bool,
    ) -> Result<()> {
        *self.receipts.entry(stream.into()).or_default() += 1;
        if self.snapshot.is_none() {
            *self.unbound.entry(stream.into()).or_default() += 1;
        }
        let Some(metadata) = metadata else {
            return self
                .engine
                .evaluate(
                    self.at(received),
                    quality_core::Scope::Group,
                    quality_core::FrameViolation::FrameContractInvalid,
                    quality_core::Condition::Violated,
                )
                .map_err(anyhow::Error::msg);
        };
        if let Some(last) = self.progress.get(stream) {
            if metadata.index <= last.0 || metadata.timestamp_us <= last.1 {
                self.source_progress_invalid = true;
            }
        }
        self.progress
            .entry(stream.into())
            .and_modify(|last| {
                last.0 = last.0.max(metadata.index);
                last.1 = last.1.max(metadata.timestamp_us);
            })
            .or_insert((metadata.index, metadata.timestamp_us));
        self.last_observed.insert(stream.into(),json!({"sequence":metadata.index,"device_timestamp_us":metadata.timestamp_us,"system_timestamp_us":metadata.system_timestamp_us,"width":metadata.width,"height":metadata.height,"format":format!("{:?}",metadata.format),"payload_bytes":metadata.data_size,"depth_scale_mm":metadata.depth_scale_mm,"received_mono_offset_ns":self.at(received).offset_ns,"received_unix_ns":received_unix_ns,"contract_valid":contract_valid}));
        let Some(snapshot) = &self.snapshot else {
            if !contract_valid {
                self.engine
                    .evaluate(
                        self.at(received),
                        quality_core::Scope::Stream(stream.into()),
                        quality_core::FrameViolation::FrameContractInvalid,
                        quality_core::Condition::Violated,
                    )
                    .map_err(anyhow::Error::msg)?;
            }
            return Ok(());
        };
        let b = &snapshot.content.binding;
        let context = capture_contract::FrameContext {
            version: 1,
            session_id: self.session.clone(),
            generation_id: b.generation_id.clone(),
            configuration_id: b.configuration_id.clone(),
            calibration_id: b.calibration_id.clone(),
            snapshot_id: snapshot.snapshot_id.clone(),
            stream_id: stream.into(),
            sequence: Some(metadata.index),
            device_timestamp_us: Some(metadata.timestamp_us),
            system_timestamp_us: Some(metadata.system_timestamp_us),
            received_mono_offset_ns: self.at(received).offset_ns,
        };
        self.engine
            .record_frame(quality_core::FrameObservation {
                context,
                contract_valid,
                valid_fraction: None,
                zero_fraction: None,
                source: Some(quality_core::SourceStamp {
                    clock: quality_core::SourceClock::Capture(
                        capture_contract::ClockDomain::SdkSystemUnixMicroseconds,
                    ),
                    value: metadata.system_timestamp_us,
                }),
                mapper: quality_core::ClockMapper::DeclaredSdkSystemUnix,
                received_unix_ns,
            })
            .map_err(anyhow::Error::msg)
    }
    fn publication(&mut self, ok: bool, completed_calls: u64) -> Result<()> {
        self.publication_calls = self.publication_calls.saturating_add(completed_calls);
        if !ok {
            self.publication_failures = self.publication_failures.saturating_add(1);
            if completed_calls > 0 {
                self.partial_publications = self.partial_publications.saturating_add(1);
            }
        }
        self.evaluate(
            quality_core::FrameViolation::PublishPartialFailure,
            if ok {
                quality_core::Condition::Healthy
            } else {
                quality_core::Condition::Violated
            },
        )
    }
    fn references(&self) -> Value {
        json!({"version":1,"state":self.engine.snapshot().state,"latest_event_sequence":self.engine.snapshot().latest_event_sequence,"current_path":self.directory.join("current.json"),"events_path":self.directory.join("events.json"),"summary_path":self.finalized.then(||self.directory.join("summary.json"))})
    }
    fn persist(&mut self) -> Result<()> {
        let current = self.engine.snapshot();
        atomic_status(
            &self.directory.join("current.json"),
            &json!({"version":1,"kind":"capture-service-quality","instance_id":self.instance,"session_id":self.session,"snapshot_id":self.snapshot.as_ref().map(|s|&s.snapshot_id),"source_age_mapping":"declared-sdk-system-unix","quality":current,"received_counts":self.receipts,"pre_snapshot_received_counts":self.unbound,"publication_calls":self.publication_calls,"publication_failures":self.publication_failures,"partial_publications":self.partial_publications,"last_observed":self.last_observed}),
        )?;
        let events = self
            .engine
            .events(None, self.policy.event_capacity)
            .map_err(anyhow::Error::msg)?;
        atomic_status(
            &self.directory.join("events.json"),
            &serde_json::to_value(events)?,
        )
    }
    fn finalize(&mut self) -> Result<()> {
        if !self.finalized {
            self.tick()?;
            let summary = self.engine.finalize_session();
            self.finalized = true;
            atomic_status(
                &self.directory.join("summary.json"),
                &json!({"version":1,"instance_id":self.instance,"summary":summary,"received_counts":self.receipts,"pre_snapshot_received_counts":self.unbound}),
            )?;
        }
        Ok(())
    }
}

fn drain_with_quality(
    quality: Option<&mut ServiceQuality>,
    metrics: &mut Metrics,
    drain: impl FnOnce(&mut Metrics) -> Result<()>,
) -> Result<()> {
    let calls_before = metrics.publish_calls;
    let result = drain(metrics);
    if let Some(quality) = quality {
        quality.publication(
            result.is_ok(),
            metrics.publish_calls.saturating_sub(calls_before),
        )?;
    }
    result
}

struct Status<'a> {
    config: &'a ServiceConfig,
    session: String,
    instance: String,
    snapshot: Option<(String, String)>,
    heartbeat: u64,
    started: Instant,
    restarts: u64,
    last_failure: Option<&'static str>,
    quality: Option<ServiceQuality>,
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
        if let Some(quality) = &mut self.quality {
            quality.persist()?;
        }
        atomic_status(
            &self.config.settings.status_path,
            &json!({"version":1,"kind":"capture-service-status","state":state,"ready":state=="ready","reason":reason,"last_failure_reason":self.last_failure,"session_id":self.session,"instance_id":self.instance,"snapshot_id":self.snapshot.as_ref().map(|s|&s.0),"snapshot_path":self.snapshot.as_ref().map(|s|&s.1),"quality":self.quality.as_ref().map(ServiceQuality::references),"generation":self.config.settings.generation,"config_sha256":self.config.hash,"pid":std::process::id(),"heartbeat_sequence":self.heartbeat,"updated_unix_ms":unix_ms(),"uptime_ms":self.started.elapsed().as_millis(),"restart_attempt":self.restarts,"last_pair_age_ms":last_pair.map(|v|v.elapsed().as_millis()),"pairs_accepted":metrics.accepted,"pairs_rejected":metrics.rejected,"pairs_published":metrics.published_pairs,"individual_publish_calls":metrics.publish_calls,"publish_failures":metrics.publish_failures,"stamp_failures":metrics.stamp_failures,"framesets_observed":metrics.framesets,"missing_pairs":metrics.missing,"skew_rejections":metrics.skew,"monotonic_rejections":metrics.monotonic,"poll_timeouts":metrics.timeouts,"physical_sync_verified":false,"delivery_verified":false}),
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
        instance: capture_contract::InstanceId::parse(
            std::env::var("GDP_CAPTURE_INSTANCE_ID").context("supervisor instance ID required")?,
        )?
        .to_string(),
        snapshot: None,
        heartbeat: 0,
        started: Instant::now(),
        restarts: restart,
        last_failure: None,
        quality: None,
    };
    status.quality = config
        .quality_runtime
        .as_ref()
        .map(|policy| {
            ServiceQuality::new(
                policy.clone(),
                &status.session,
                &status.instance,
                status.started,
                &config.settings.status_path,
            )
        })
        .transpose()?;
    let mut metrics = Metrics::default();
    let mut last_pair = None;
    status.write("starting", None, &metrics, last_pair)?;
    let outcome = (|| -> Result<()> {
        ensure!(
            cfg!(target_os = "linux") && cfg!(target_endian = "little"),
            "supported SDK host required"
        );
        config.verify_runtime()?;
        let sdk = match orbbec_safe::Sdk::load(&config.capture.sdk_library) {
            Ok(sdk) => sdk,
            Err(error) => {
                if let Some(quality) = &mut status.quality {
                    quality.evaluate(
                        quality_core::FrameViolation::DeviceDisconnected,
                        quality_core::Condition::Violated,
                    )?;
                }
                return Err(error.into());
            }
        };
        let mut pipeline = match sdk.open(
            &config.capture.serial,
            Some(config.capture.color.profile),
            Some(config.capture.depth.profile),
        ) {
            Ok(pipeline) => pipeline,
            Err(error) => {
                if let Some(quality) = &mut status.quality {
                    quality.evaluate(
                        quality_core::FrameViolation::DeviceDisconnected,
                        quality_core::Condition::Violated,
                    )?;
                }
                return Err(error.into());
            }
        };
        let mut ros = None;
        let mut queue = LatestQueue::new(config.capture.capacity)?;
        let mut guard = PairGuard::default();
        let mut bound_guard: Option<frame_core::BoundPairGuard> = None;
        let opened = Instant::now();
        let warmup = Duration::from_secs_f64(config.capture.startup_discovery_seconds);
        let mut heartbeat = Instant::now();
        let mut consecutive = 0_u64;
        let mut had_ready = false;
        let mut sequence = 0_u64;
        let result = (|| -> Result<()> {
            while !STOP.load(Ordering::Relaxed) {
                if let Some(quality) = &mut status.quality {
                    quality.tick()?;
                }
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
                let set = match pipeline.wait(config.capture.poll_ms) {
                    Ok(Some(set)) => set,
                    Ok(None) => {
                        metrics.timeouts += 1;
                        continue;
                    }
                    Err(error) => {
                        if let Some(quality) = &mut status.quality {
                            quality.evaluate(
                                quality_core::FrameViolation::DeviceDisconnected,
                                quality_core::Condition::Violated,
                            )?;
                        }
                        return Err(error.into());
                    }
                };
                sequence += 1;
                metrics.framesets += 1;
                let received = Instant::now();
                let color = set.color()?;
                let depth = set.depth()?;
                let received_unix_ns = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .ok()
                    .and_then(|d| u64::try_from(d.as_nanos()).ok());
                let color_metadata = color.as_ref().and_then(|f| f.metadata().ok());
                let depth_metadata = depth.as_ref().and_then(|f| f.metadata().ok());
                if let Some(quality) = &mut status.quality {
                    quality.source_progress_invalid = false;
                    for (stream, frame, metadata, profile, format) in [
                        (
                            "color",
                            color.as_ref(),
                            color_metadata.as_ref(),
                            config.capture.color.profile,
                            orbbec_safe::Format::Yuyv,
                        ),
                        (
                            "depth",
                            depth.as_ref(),
                            depth_metadata.as_ref(),
                            config.capture.depth.profile,
                            orbbec_safe::Format::Depth16,
                        ),
                    ] {
                        if frame.is_some() {
                            let valid = metadata.is_some_and(|m| {
                                m.format == format
                                    && m.width == profile.width
                                    && m.height == profile.height
                                    && (m.width as usize)
                                        .checked_mul(m.height as usize)
                                        .and_then(|n| n.checked_mul(2))
                                        == Some(m.data_size)
                                    && (stream != "depth"
                                        || m.depth_scale_mm
                                            .is_some_and(|v| v.is_finite() && v > 0.))
                            });
                            quality.receipt(stream, metadata, received, received_unix_ns, valid)?;
                        }
                    }
                    if quality.source_progress_invalid {
                        quality.evaluate(
                            quality_core::FrameViolation::TimestampRegression,
                            quality_core::Condition::Violated,
                        )?;
                    }
                    if let (Some(c), Some(d)) = (&color_metadata, &depth_metadata) {
                        let (c, d) = match config.capture.policy.clock {
                            frame_core::PairClock::Device => (c.timestamp_us, d.timestamp_us),
                            frame_core::PairClock::System => {
                                (c.system_timestamp_us, d.system_timestamp_us)
                            }
                        };
                        quality
                            .engine
                            .observe_pair_delta(quality.at(Instant::now()), c.abs_diff(d))
                            .map_err(anyhow::Error::msg)?;
                    }
                }
                if color.is_none() || depth.is_none() {
                    metrics.missing += 1;
                    metrics.reject("MISSING_FRAME");
                    if let Some(quality) = &mut status.quality {
                        quality.evaluate(
                            quality_core::FrameViolation::FrameContractInvalid,
                            quality_core::Condition::Violated,
                        )?;
                    }
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
                        if let Some(quality) = &mut status.quality {
                            quality.evaluate(
                                quality_core::FrameViolation::FrameContractInvalid,
                                quality_core::Condition::Violated,
                            )?;
                        }
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
                if bound_guard.is_none() && config.capture.quality_policy.is_some() {
                    let instance = std::env::var("GDP_CAPTURE_INSTANCE_ID")
                        .context("explicit runtime instance required for pair evidence")?;
                    let snapshot = crate::capture_snapshot::build(
                        &config.capture,
                        &set.calibration()?,
                        crate::capture_snapshot::RuntimeBinding {
                            generation: &config.settings.generation,
                            session: &status.session,
                            instance: &instance,
                            runtime_sha: &config.hash,
                            runtime_contract: config.runtime_contract.as_ref(),
                        },
                        depth
                            .metadata
                            .source_depth_scale_mm
                            .context("depth scale required")?,
                    )?;
                    let directory = config
                        .settings
                        .status_path
                        .parent()
                        .context("status directory")?
                        .join("snapshots");
                    let store = capture_contract::store::SnapshotStore::open(&directory)?;
                    let id = store.publish(&snapshot)?;
                    status.snapshot = Some((
                        id.to_string(),
                        directory
                            .join(format!("{id}.json"))
                            .to_string_lossy()
                            .into(),
                    ));
                    if let Some(quality) = &mut status.quality {
                        quality.bind(&snapshot)?;
                    }
                    bound_guard = Some(frame_core::BoundPairGuard::new(snapshot, status.started)?);
                }
                let lengths = (color.payload.data()?.len(), depth.payload.data()?.len());
                let result = if let Some(guard) = bound_guard.as_mut() {
                    guard
                        .accept(
                            sequence,
                            Some(color),
                            Some(depth),
                            lengths,
                            config.capture.policy,
                            PairProvenance::CallerObservedSdkFrameset,
                        )
                        .map(|b| (b.pair, Some(b.evidence)))
                        .map_err(|e| match e {
                            frame_core::BoundPairError::Pair(p) => p,
                            _ => frame_core::PairError::InvalidPolicy,
                        })
                } else {
                    guard
                        .accept(
                            sequence,
                            Some(color),
                            Some(depth),
                            lengths,
                            config.capture.policy,
                            PairProvenance::CallerObservedSdkFrameset,
                        )
                        .map(|pair| (pair, None))
                };
                let (pair, bound_evidence) = match result {
                    Ok(value) => value,
                    Err(error) => {
                        if matches!(error, frame_core::PairError::SkewExceeded) {
                            metrics.skew += 1;
                        } else {
                            metrics.monotonic += 1;
                        }
                        metrics.reject("PAIR_CONTRACT_REJECTED");
                        if let Some(quality) = &mut status.quality {
                            // Raw pair delta/progression were already observed once
                            // for this frameset; a guard rejection must not count a
                            // second observation or shorten configured hysteresis.
                            if !matches!(error, frame_core::PairError::SkewExceeded)
                                && !quality.source_progress_invalid
                            {
                                quality.evaluate(
                                    quality_core::FrameViolation::TimestampRegression,
                                    quality_core::Condition::Violated,
                                )?;
                            }
                        }
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
                if let Some(quality) = &mut status.quality {
                    quality.evaluate(
                        quality_core::FrameViolation::FrameContractInvalid,
                        quality_core::Condition::Healthy,
                    )?;
                    if !quality.source_progress_invalid {
                        quality.evaluate(
                            quality_core::FrameViolation::TimestampRegression,
                            quality_core::Condition::Healthy,
                        )?;
                    }
                }
                let evicted = queue.push(QueuedPair {
                    bound_evidence,
                    pair,
                    enqueued_at: Instant::now(),
                });
                if let Some(quality) = &mut status.quality {
                    quality.evaluate(
                        quality_core::FrameViolation::QueueOverflow,
                        if evicted.is_some() {
                            quality_core::Condition::Violated
                        } else {
                            quality_core::Condition::Healthy
                        },
                    )?;
                }
                drop(evicted);
                drain_with_quality(status.quality.as_mut(), &mut metrics, |metrics| {
                    rgbd::drain(&mut queue, &mut ros, metrics)
                })?;
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
    if let Some(quality) = &mut status.quality {
        if outcome.is_err() && quality.engine.snapshot().state == quality_core::QualityState::Normal
        {
            quality.evaluate(
                quality_core::FrameViolation::FrameContractInvalid,
                quality_core::Condition::Violated,
            )?;
        }
        quality.finalize()?;
    }
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
    instance_id: &str,
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
    value["instance_id"] = instance_id.into();
    value["restart_attempt"] = restarts.into();
    value["updated_unix_ms"] = unix_ms().into();
    atomic_status(&config.settings.status_path, &value)
}
// Any present/unreadable gate is protection, including malformed/symlink input.
fn recording_blocks_restart(path: Option<&Path>) -> bool {
    path.is_some_and(
        |p| !matches!(fs::symlink_metadata(p), Err(e) if e.kind() == std::io::ErrorKind::NotFound),
    )
}
fn acknowledge_recording(config: &ServiceConfig, worker_pid: u32) {
    let Some(path) = config.settings.recording_control_path.as_deref() else {
        return;
    };
    let operation = (|| -> Result<()> {
        let metadata = fs::symlink_metadata(path)?;
        ensure!(
            metadata.is_file() && !metadata.file_type().is_symlink() && metadata.len() <= 65536,
            "recording control file invalid"
        );
        let request = capture_contract::recording::GateRequest::parse(&fs::read(path)?)?;
        let status: Value =
            serde_json::from_str(&fs::read_to_string(&config.settings.status_path)?)?;
        let ack = request.acknowledge(&status, worker_pid, std::process::id(), &config.hash)?;
        let ack = serde_json::to_value(ack)?;
        let ack_path = path.with_file_name("ack.json");
        if fs::read(&ack_path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .as_ref()
            == Some(&ack)
        {
            return Ok(());
        }
        atomic_status(&ack_path, &ack)
    })();
    // Missing, malformed or stale controls are never acknowledged. Presence
    // still prevents recovery; a management timeout keeps its durable guard.
    let _ = operation;
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
    let instance_id = format!("{}{}", random_session()?, random_session()?);
    let mut restarts = 0_u64;
    loop {
        ensure!(
            sha(&fs::read(path)?) == config.hash,
            "immutable config changed"
        );
        if STOP.load(Ordering::Relaxed) {
            supervisor_status(config, "stopped", "SIGNAL", restarts, &instance_id)?;
            return Ok(());
        }
        while recording_blocks_restart(config.settings.recording_control_path.as_deref()) {
            supervisor_status(
                config,
                "degraded",
                "RECORDING_PROTECTED_RECOVERY_BLOCKED",
                restarts,
                &instance_id,
            )?;
            if STOP.load(Ordering::Relaxed) {
                supervisor_status(config, "stopped", "SIGNAL", restarts, &instance_id)?;
                return Ok(());
            }
            wait_interruptible(Duration::from_millis(100));
        }
        let mut child = Command::new(std::env::current_exe()?)
            .env("GDP_CAPTURE_INSTANCE_ID", &instance_id)
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
            acknowledge_recording(config, child.id());
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
            &instance_id,
        )?;
        if STOP.load(Ordering::Relaxed) {
            // A signal can arrive after the degraded snapshot above was written.
            // A successful shutdown must commit its final state before returning.
            supervisor_status(config, "stopped", "SIGNAL", restarts, &instance_id)?;
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
    fn runtime_policy() -> quality_core::RuntimeQualityPolicy {
        quality_core::RuntimeQualityPolicy {
            version: 1,
            quality: capture_contract::QualityPolicy {
                version: 1,
                max_source_age_ns: 1_000_000_000,
                max_future_ns: 100_000_000,
                max_pair_delta_us: 20_000,
                min_valid_fraction: 0.,
                max_zero_fraction: 1.,
                action: capture_contract::QualityAction::ReportOnly,
            },
            stale_after_ns: 1_000_000,
            event_capacity: 8,
            window_capacity: 8,
            window_horizon_ns: 10_000_000_000,
            summary_capacity: 1,
            max_sessions: 1,
            rules: quality_core::FrameViolation::ALL
                .into_iter()
                .map(|v| {
                    (
                        v,
                        quality_core::RulePolicy {
                            open_after: 1,
                            recover_after: 2,
                            severity: quality_core::QualityState::Degraded,
                        },
                    )
                })
                .collect(),
        }
    }
    fn quality_snapshot(
        session: &str,
        instance: &str,
    ) -> capture_contract::snapshot::SnapshotEnvelope {
        use capture_contract::{snapshot::*, *};
        let session = SessionId::parse(session).unwrap();
        let instance = InstanceId::parse(instance).unwrap();
        let calibration = CalibrationRecord {
            version: 1,
            source: CalibrationSource::SdkActiveProfiles,
            actual_runtime_observed: true,
            source_session_id: Some(session.clone()),
            source_instance_id: Some(instance.clone()),
            observed_unix_ns: Some(1),
            models: [("color", "color_frame"), ("depth", "depth_frame")]
                .into_iter()
                .map(|(name, frame)| {
                    (
                        name.into(),
                        CameraModel {
                            frame_id: frame.into(),
                            width: 2,
                            height: 1,
                            k: [1., 0., 0., 0., 1., 0., 0., 0., 1.],
                            distortion_model: DistortionModel::None,
                            distortion_coefficients: vec![],
                        },
                    )
                })
                .collect(),
            transforms: vec![],
            physical_accuracy_verified: false,
            legacy_source_sha256: None,
        };
        let configuration = json!({"synthetic_test_fixture":true});
        CaptureSnapshot {
            version: 1,
            binding: SnapshotBinding {
                instance_id: instance,
                session_id: session,
                generation_id: GenerationId::parse("c".repeat(32)).unwrap(),
                configuration_id: ConfigurationId::parse(
                    content_digest(
                        ContentKind::Configuration,
                        &serde_json::to_vec(&configuration).unwrap(),
                    )
                    .unwrap(),
                )
                .unwrap(),
                calibration_id: calibration.id().unwrap(),
            },
            configuration,
            calibration,
            depth: DepthRepresentation::Millimetres {
                quantization:
                    QuantizationPolicy::NearestHalfUpPreserveZeroClampPositiveRejectOverflow,
            },
            quality_policy: runtime_policy().quality,
        }
        .seal()
        .unwrap()
    }
    #[test]
    fn service_quality_policy_is_explicit_consistent_and_snapshot_configuration_bound() {
        let mut value = config();
        let policy = runtime_policy();
        value["quality_policy"] = serde_json::to_value(&policy.quality).unwrap();
        value["quality_runtime"] = serde_json::to_value(&policy).unwrap();
        let parsed = ServiceConfig::parse(&value.to_string()).unwrap();
        assert_eq!(
            parsed.capture.configuration["quality_runtime"],
            value["quality_runtime"]
        );
        value["quality_runtime"]["quality"]["max_pair_delta_us"] = json!(1);
        assert!(ServiceConfig::parse(&value.to_string()).is_err());
        value["quality_runtime"] = serde_json::to_value(&policy).unwrap();
        value["quality_runtime"]["event_capacity"] = json!(0);
        assert!(ServiceConfig::parse(&value.to_string()).is_err());
    }
    #[test]
    fn service_adapter_receipts_staleness_partial_publication_and_private_summary_are_real() {
        let dir =
            std::env::temp_dir().join(format!("quality-adapter-{}", random_session().unwrap()));
        let session = "a".repeat(32);
        let instance = "b".repeat(64);
        let mut quality = ServiceQuality::new(
            runtime_policy(),
            &session,
            &instance,
            Instant::now(),
            &dir.join("status.json"),
        )
        .unwrap();
        let mut metadata = orbbec_safe::FrameMetadata {
            format: orbbec_safe::Format::Depth16,
            width: 2,
            height: 1,
            index: 1,
            timestamp_us: 10,
            system_timestamp_us: 1_000_000,
            data_size: 4,
            depth_scale_mm: Some(1.),
        };
        quality
            .receipt(
                "depth",
                Some(&metadata),
                Instant::now(),
                Some(1_000_000_000),
                true,
            )
            .unwrap();
        assert_eq!(quality.receipts["depth"], 1);
        assert_eq!(quality.unbound["depth"], 1);
        assert_eq!(quality.engine.snapshot().streams["depth"].frame_count, 0);
        quality
            .bind(&quality_snapshot(&session, &instance))
            .unwrap();
        metadata.index = 2;
        metadata.timestamp_us = 20;
        metadata.system_timestamp_us += 1;
        quality
            .receipt(
                "depth",
                Some(&metadata),
                Instant::now(),
                Some(1_000_001_000),
                true,
            )
            .unwrap();
        assert_eq!(quality.engine.snapshot().streams["depth"].frame_count, 1);
        let mut metrics = Metrics::default();
        assert!(
            drain_with_quality(Some(&mut quality), &mut metrics, |metrics| {
                metrics.publish_calls += 1;
                anyhow::bail!("synthetic second publisher failed")
            })
            .is_err()
        );
        assert!(
            drain_with_quality(Some(&mut quality), &mut metrics, |metrics| {
                metrics.publish_calls += 2;
                anyhow::bail!("synthetic metadata publisher failed")
            })
            .is_err()
        );
        assert_eq!(quality.partial_publications, 2);
        assert_eq!(quality.publication_calls, 3);
        let events = quality.engine.events(None, 8).unwrap();
        assert_eq!(
            events
                .events
                .iter()
                .filter(|e| e.violation == quality_core::FrameViolation::PublishPartialFailure)
                .count(),
            1
        );
        quality.publication(true, 4).unwrap();
        assert!(
            quality
                .engine
                .snapshot()
                .active_episodes
                .iter()
                .any(|e| e.violation == quality_core::FrameViolation::PublishPartialFailure)
        );
        quality.publication(true, 4).unwrap();
        assert!(
            !quality
                .engine
                .snapshot()
                .active_episodes
                .iter()
                .any(|e| e.violation == quality_core::FrameViolation::PublishPartialFailure)
        );
        thread::sleep(Duration::from_millis(2));
        quality.tick().unwrap();
        assert!(
            quality
                .engine
                .snapshot()
                .active_episodes
                .iter()
                .any(|e| e.violation == quality_core::FrameViolation::StreamStale)
        );
        quality.persist().unwrap();
        quality.finalize().unwrap();
        quality.persist().unwrap();
        let current: Value =
            serde_json::from_slice(&fs::read(quality.directory.join("current.json")).unwrap())
                .unwrap();
        assert_eq!(current["received_counts"]["depth"], 2);
        assert_eq!(current["quality"]["current_session_finalized"], true);
        assert_eq!(current["last_observed"]["depth"]["sequence"], 2);
        assert_eq!(current["partial_publications"], 2);
        assert!(quality.directory.join("summary.json").is_file());
        assert!(quality.directory.join("events.json").is_file());
        assert_eq!(
            fs::metadata(quality.directory.join("current.json"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        fs::remove_dir_all(dir).unwrap();
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
    fn any_present_recording_control_blocks_restart_including_corrupt_or_symlink() {
        let directory =
            std::env::temp_dir().join(format!("capture-gate-test-{}", random_session().unwrap()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("control.json");
        assert!(!recording_blocks_restart(Some(&path)));
        fs::write(&path, b"malformed").unwrap();
        assert!(recording_blocks_restart(Some(&path)));
        fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(directory.join("missing"), &path).unwrap();
        assert!(recording_blocks_restart(Some(&path)));
        fs::remove_dir_all(directory).unwrap();
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
            instance: "c".repeat(64),
            snapshot: None,
            heartbeat: 0,
            started: Instant::now(),
            restarts: 0,
            last_failure: None,
            quality: None,
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
