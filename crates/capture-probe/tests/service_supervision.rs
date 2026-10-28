//! Linux-only black-box process supervision. The native ABI fixtures never open
//! USB, a real SDK device or ROS. macOS deliberately rejects live workers.
#![cfg(target_os = "linux")]
use ring::digest;
use serde_json::{Value, json};
use std::{
    fmt::Write,
    fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    root: PathBuf,
    config: PathBuf,
    status: PathBuf,
    events: PathBuf,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn compile(compiler: &str, source: &Path, output: &Path, include: Option<&Path>) {
    let mut command = Command::new(compiler);
    command.args(["-shared", "-fPIC", "-Wall", "-Wextra", "-Werror"]);
    command.arg(if compiler == "cc" {
        "-std=c11"
    } else {
        "-std=c++17"
    });
    if let Some(include) = include {
        command.arg("-I").arg(include);
    }
    let out = command
        .arg(source)
        .arg("-o")
        .arg(output)
        .output()
        .expect("C/C++ compiler required for process ABI fixture");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
fn replace(source: &mut String, old: &str, new: &str) {
    assert!(
        source.contains(old),
        "ABI fixture changed; review service adaptation"
    );
    *source = source.replace(old, new);
}
fn fixture() -> Fixture {
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let root = std::env::temp_dir().join(format!(
        "gdp-supervisor-{}-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&root).unwrap();
    let sdk = root.join("mock-sdk.so");
    let bridge = root.join("mock-bridge.so");
    let mut sdk_source =
        fs::read_to_string(crate_root.join("../orbbec-safe/tests/mock_sdk.c")).unwrap();
    sdk_source.insert_str(0,"#define _DEFAULT_SOURCE\n#include <unistd.h>\nstatic unsigned long long service_sequence;\n");
    replace(
        &mut sdk_source,
        "if(ms==1)return NULL;return make(5);",
        "if(ms==1)return NULL;usleep(10000);service_sequence++;return make(5);",
    );
    replace(
        &mut sdk_source,
        "(void)d;(void)e;return make(3);",
        "(void)d;(void)e;const char *delay=getenv(\"GDP_MOCK_OPEN_DELAY_MS\");if(delay)usleep((useconds_t)(strtoul(delay,NULL,10)*1000));return make(3);",
    );
    replace(&mut sdk_source, "return 42;", "return service_sequence;");
    replace(
        &mut sdk_source,
        "return 1234;",
        "return service_sequence*33333;",
    );
    replace(
        &mut sdk_source,
        "return 5678;",
        "return 1700000000000000ULL+service_sequence*33333;",
    );
    let sdk_c = root.join("mock-sdk.c");
    fs::write(&sdk_c, sdk_source).unwrap();
    compile("cc", &sdk_c, &sdk, None);
    let mut bridge_source =
        fs::read_to_string(crate_root.join("../ros-output/tests/mock_bridge.cpp")).unwrap();
    replace(
        &mut bridge_source,
        "if (frame->sequence == 1 && (!frame->has_device_timestamp || frame->device_timestamp_us != 0)) return 2;",
        "",
    );
    replace(
        &mut bridge_source,
        "if (frame->sequence == 999) { event(\"publish-failed\"); return 2; }",
        "",
    );
    replace(
        &mut bridge_source,
        "if (!p.owned.empty() && p.owned[0] != 16) return 2;",
        "",
    );
    replace(
        &mut bridge_source,
        "if (p.frame_id != \"test_optical\" || p.focal != 1.) return 2;",
        "",
    );
    replace(
        &mut bridge_source,
        "if(pair->pair_sequence==999){event(\"aux-publish-failed\");return 2;}",
        "",
    );
    let bridge_cpp = root.join("mock-bridge.cpp");
    fs::write(&bridge_cpp, bridge_source).unwrap();
    compile(
        "c++",
        &bridge_cpp,
        &bridge,
        Some(&crate_root.join("../../ros/include")),
    );
    let status = root.join("status.json");
    let config = root.join("config.json");
    let events = root.join("events");
    fs::write(&config,json!({"version":1,"sdk_library":sdk,"serial":"test-device","color":{"width":2,"height":2,"fps":30,"frame_id":"color_optical"},"depth":{"width":2,"height":2,"fps":30,"frame_id":"depth_optical","unit_policy":"normalize-mm"},"pairing":{"clock":"device","max_delta_us":20000},"poll_ms":10,"capacity":2,"ros":{"bridge":bridge,"namespace":"/service_test","stamp_source":"sdk-system-unix"},"service":{"status_path":status,"generation":"fixture-generation","idle_timeout_ms":500,"startup_timeout_ms":5000,"heartbeat_ms":50,"heartbeat_timeout_ms":500,"max_restarts":1,"restart_backoff_ms":100,"shutdown_timeout_ms":300,"ready_min_pairs":2}}).to_string()).unwrap();
    Fixture {
        root,
        config,
        status,
        events,
    }
}
fn launch(f: &Fixture) -> Child {
    Command::new(env!("CARGO_BIN_EXE_capture-service"))
        .arg("--config")
        .arg(&f.config)
        .env("GEMINIDEPTHPORT_MOCK_EVENTS", &f.events)
        .env("GDP_ROS_MOCK_EVENTS", &f.events)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}
fn status(f: &Fixture) -> Option<Value> {
    fs::read_to_string(&f.status)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
}
fn wait_status(f: &Fixture, child: &mut Child, predicate: impl Fn(&Value) -> bool) -> Value {
    let start = Instant::now();
    loop {
        if let Some(value) = status(f) {
            if predicate(&value) {
                return value;
            }
        }
        assert!(
            child.try_wait().unwrap().is_none(),
            "service exited before expected state: {:?}",
            status(f)
        );
        if start.elapsed() > Duration::from_secs(8) {
            unsafe {
                libc::kill(child.id() as i32, libc::SIGTERM);
            }
            let _ = child.wait();
            panic!("status deadline: {:?}", status(f));
        }
        thread::sleep(Duration::from_millis(10));
    }
}
fn alive(pid: u64) -> bool {
    unsafe { libc::kill(pid as i32, 0) == 0 }
}
fn stop(child: &mut Child) {
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    let started = Instant::now();
    loop {
        if let Some(exit) = child.try_wait().unwrap() {
            assert!(exit.success());
            return;
        }
        assert!(started.elapsed() < Duration::from_secs(4));
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn stopped_worker_is_bounded_killed_then_new_session_recovers_without_config_change() {
    let f = fixture();
    let original = fs::read(&f.config).unwrap();
    let mut supervisor = launch(&f);
    let first = wait_status(&f, &mut supervisor, |v| v["state"] == "ready");
    let old_pid = first["pid"].as_u64().unwrap();
    let old_session = first["session_id"].as_str().unwrap().to_owned();
    unsafe {
        assert_eq!(libc::kill(old_pid as i32, libc::SIGSTOP), 0);
    }
    let stopped = Instant::now();
    thread::sleep(Duration::from_millis(180));
    // Atomic status is a snapshot, not a live readiness lease. A stopped worker
    // may still have ready=true before watchdog expiry, but heartbeat is stale.
    let stale = status(&f).unwrap();
    assert_eq!(stale["session_id"], old_session);
    assert_eq!(stale["ready"], true);
    let age = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
        - stale["updated_unix_ms"].as_u64().unwrap();
    assert!(age >= 100, "health must reject stale ready snapshot");
    let second = wait_status(&f, &mut supervisor, |v| {
        v["state"] == "ready" && v["session_id"].as_str().is_some_and(|s| s != old_session)
    });
    assert!(stopped.elapsed() < Duration::from_secs(4));
    assert!(!alive(old_pid), "old stopped worker was not reaped");
    assert_ne!(second["pid"].as_u64().unwrap(), old_pid);
    assert_eq!(second["restart_attempt"], 1);
    assert_eq!(fs::read(&f.config).unwrap(), original);
    let new_pid = second["pid"].as_u64().unwrap();
    stop(&mut supervisor);
    assert!(!alive(new_pid));
    assert_eq!(status(&f).unwrap()["state"], "stopped");
    let events = fs::read_to_string(&f.events).unwrap();
    assert_eq!(events.lines().filter(|l| *l == "start").count(), 2);
    assert!(events.lines().any(|l| l == "stop"));
}

#[test]
fn sigterm_cleans_worker_native_handles_and_child_process() {
    let f = fixture();
    let mut supervisor = launch(&f);
    let ready = wait_status(&f, &mut supervisor, |v| v["state"] == "ready");
    let worker = ready["pid"].as_u64().unwrap();
    stop(&mut supervisor);
    assert!(!alive(worker));
    let events = fs::read_to_string(&f.events).unwrap();
    for event in [
        "stop",
        "pipeline-delete",
        "device-delete",
        "context-delete",
        "aux-destroy",
        "destroy",
    ] {
        assert!(
            events.lines().any(|l| l == event),
            "missing native cleanup {event}"
        );
    }
    assert_eq!(status(&f).unwrap()["reason"], "SIGNAL");
}

#[test]
fn wrong_runtime_hash_fails_before_any_sdk_start() {
    let f = fixture();
    let mut config: Value = serde_json::from_slice(&fs::read(&f.config).unwrap()).unwrap();
    config["runtime_contract"] = json!({"sdk_sha256":"0".repeat(64),"bridge_sha256":"0".repeat(64),"service_sha256":"0".repeat(64)});
    config["service"]["max_restarts"] = json!(0);
    fs::write(&f.config, config.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_capture-service"))
        .arg("--validate-config")
        .arg(&f.config)
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!String::from_utf8_lossy(&out.stderr).contains("test-device"));
    let mut supervisor = launch(&f);
    let start = Instant::now();
    loop {
        if let Some(exit) = supervisor.try_wait().unwrap() {
            assert!(!exit.success());
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(4));
        thread::sleep(Duration::from_millis(10));
    }
    assert!(fs::read_to_string(&f.events).unwrap_or_default().is_empty());
    assert_eq!(status(&f).unwrap()["ready"], false);
}

#[test]
fn config_changed_during_failure_cannot_be_adopted_by_restart() {
    let f = fixture();
    let mut supervisor = launch(&f);
    let ready = wait_status(&f, &mut supervisor, |v| v["state"] == "ready");
    let old_pid = ready["pid"].as_u64().unwrap();
    let mut value: Value = serde_json::from_slice(&fs::read(&f.config).unwrap()).unwrap();
    value["service"]["ready_min_pairs"] = json!(3);
    fs::write(&f.config, value.to_string()).unwrap();
    unsafe {
        libc::kill(old_pid as i32, libc::SIGSTOP);
    }
    let start = Instant::now();
    loop {
        if let Some(exit) = supervisor.try_wait().unwrap() {
            assert!(!exit.success());
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(4));
        thread::sleep(Duration::from_millis(10));
    }
    assert!(!alive(old_pid));
    assert_eq!(
        fs::read_to_string(&f.events)
            .unwrap()
            .lines()
            .filter(|l| *l == "start")
            .count(),
        1
    );
    assert_eq!(status(&f).unwrap()["ready"], false);
}

#[test]
fn correct_artifact_hashes_validate_without_loading_native_libraries() {
    let f = fixture();
    let mut value: Value = serde_json::from_slice(&fs::read(&f.config).unwrap()).unwrap();
    let hash = |path: &Path| {
        digest::digest(&digest::SHA256, &fs::read(path).unwrap())
            .as_ref()
            .iter()
            .fold(String::new(), |mut output, byte| {
                write!(&mut output, "{byte:02x}").unwrap();
                output
            })
    };
    value["runtime_contract"] = json!({"sdk_sha256":hash(Path::new(value["sdk_library"].as_str().unwrap())),"bridge_sha256":hash(Path::new(value["ros"]["bridge"].as_str().unwrap())),"service_sha256":hash(Path::new(env!("CARGO_BIN_EXE_capture-service")))});
    fs::write(&f.config, value.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_capture-service"))
        .arg("--validate-config")
        .arg(&f.config)
        .output()
        .unwrap();
    assert!(out.status.success());
    assert!(fs::read_to_string(&f.events).unwrap_or_default().is_empty());
}

#[test]
fn sigkill_supervisor_cannot_leave_running_sdk_worker_orphan() {
    let f = fixture();
    let mut supervisor = launch(&f);
    let ready = wait_status(&f, &mut supervisor, |v| v["state"] == "ready");
    let worker_pid = ready["pid"].as_u64().unwrap();
    unsafe {
        assert_eq!(libc::kill(supervisor.id() as i32, libc::SIGKILL), 0);
    }
    let exit = supervisor.wait().unwrap();
    assert!(!exit.success());
    let started = Instant::now();
    while alive(worker_pid) {
        assert!(
            started.elapsed() < Duration::from_secs(4),
            "SDK worker survived parent SIGKILL"
        );
        thread::sleep(Duration::from_millis(10));
    }
    // No process can refresh the old ready snapshot now. Health must reject its
    // timestamp; the process death signal does not pretend to perform SDK stop.
    let old = status(&f).unwrap();
    assert_eq!(old["session_id"], ready["session_id"]);
}

#[test]
fn worker_rejects_unrelated_or_already_dead_expected_parent_before_sdk_access() {
    let f = fixture();
    let hash = digest::digest(&digest::SHA256, &fs::read(&f.config).unwrap())
        .as_ref()
        .iter()
        .fold(String::new(), |mut output, byte| {
            write!(&mut output, "{byte:02x}").unwrap();
            output
        });
    let out = Command::new(env!("CARGO_BIN_EXE_capture-service"))
        .arg("--worker")
        .arg("--config")
        .arg(&f.config)
        .args([
            "--expected-config-sha",
            &hash,
            "--restart-attempt",
            "0",
            "--supervisor-pid",
            "2147483647",
        ])
        .env("GEMINIDEPTHPORT_MOCK_EVENTS", &f.events)
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!f.status.exists());
    assert!(!f.events.exists());
}

#[test]
fn slow_sdk_open_cannot_extend_worker_readiness_startup_budget() {
    let f = fixture();
    let mut value: Value = serde_json::from_slice(&fs::read(&f.config).unwrap()).unwrap();
    value["service"]["startup_timeout_ms"] = json!(1000);
    value["service"]["heartbeat_timeout_ms"] = json!(4000);
    value["service"]["max_restarts"] = json!(0);
    fs::write(&f.config, value.to_string()).unwrap();
    let mut supervisor = Command::new(env!("CARGO_BIN_EXE_capture-service"))
        .arg("--config")
        .arg(&f.config)
        .env("GEMINIDEPTHPORT_MOCK_EVENTS", &f.events)
        .env("GDP_ROS_MOCK_EVENTS", &f.events)
        .env("GDP_MOCK_OPEN_DELAY_MS", "2000")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let start = Instant::now();
    loop {
        if status(&f).is_some_and(|v| v["ready"] == true) {
            stop(&mut supervisor);
            panic!("slow SDK open incorrectly received a fresh readiness budget");
        }
        if let Some(exit) = supervisor.try_wait().unwrap() {
            assert!(!exit.success());
            break;
        }
        if start.elapsed() > Duration::from_secs(5) {
            stop(&mut supervisor);
            panic!("slow open test exceeded bounded watchdog");
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert!(start.elapsed() >= Duration::from_secs(2));
    let value = status(&f).unwrap();
    assert_eq!(value["ready"], false);
    assert_eq!(value["pairs_published"], 0);
    let events = fs::read_to_string(&f.events).unwrap();
    assert_eq!(events.lines().filter(|l| *l == "start").count(), 1);
    assert_eq!(events.lines().filter(|l| *l == "stop").count(), 1);
    assert!(!events.lines().any(|l| l == "publish-copied"));
}
