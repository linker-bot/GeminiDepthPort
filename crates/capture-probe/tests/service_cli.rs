use serde_json::{Value, json};
use std::{
    fs,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant},
};
static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);
struct ChildGuard(Child);
impl std::ops::Deref for ChildGuard {
    type Target = Child;
    fn deref(&self) -> &Child {
        &self.0
    }
}
impl std::ops::DerefMut for ChildGuard {
    fn deref_mut(&mut self) -> &mut Child {
        &mut self.0
    }
}
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_some() {
            return;
        }
        unsafe {
            libc::kill(self.0.id() as i32, libc::SIGTERM);
        };
        for _ in 0..200 {
            if self.0.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(10));
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn fixture() -> (std::path::PathBuf, Value) {
    let directory = std::env::temp_dir().join(format!(
        "capture-service-cli-{}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir(&directory).unwrap();
    let value = json!({"version":1,"sdk_library":"/private/missing-sdk.so","serial":"private-device-never-output","color":{"width":640,"height":480,"fps":30,"frame_id":"color_optical"},"depth":{"width":640,"height":480,"fps":30,"frame_id":"depth_optical","unit_policy":"normalize-mm"},"pairing":{"clock":"device","max_delta_us":20000},"poll_ms":100,"capacity":4,"ros":{"bridge":"/private/missing-bridge.so","namespace":"/test","stamp_source":"sdk-system-unix"},"service":{"status_path":directory.join("status.json"),"generation":"test-generation","idle_timeout_ms":2000,"startup_timeout_ms":15000,"heartbeat_ms":500,"heartbeat_timeout_ms":10000,"max_restarts":0,"restart_backoff_ms":1000,"shutdown_timeout_ms":500,"ready_min_pairs":5}});
    (directory, value)
}
#[test]
fn config_validation_never_opens_device() {
    let (dir, value) = fixture();
    let path = dir.join("config.json");
    fs::write(&path, value.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_capture-service"))
        .arg("--validate-config")
        .arg(path)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "status={} stderr={}",
        out.status,
        String::from_utf8_lossy(&out.stderr)
    );
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["valid"], true);
    assert_eq!(v["hardware_verified"], false);
    assert!(!dir.join("status.json").exists());
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn failed_worker_has_finite_restart_budget_and_private_status() {
    let (dir, value) = fixture();
    let path = dir.join("config.json");
    fs::write(&path, value.to_string()).unwrap();
    let mut child = ChildGuard(
        Command::new(env!("CARGO_BIN_EXE_capture-service"))
            .arg("--config")
            .arg(path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let start = Instant::now();
    let exit = loop {
        if let Some(exit) = child.try_wait().unwrap() {
            break exit;
        }
        assert!(start.elapsed() < Duration::from_secs(5));
        thread::sleep(Duration::from_millis(25));
    };
    assert!(!exit.success());
    let raw = fs::read_to_string(dir.join("status.json")).unwrap();
    assert!(!raw.contains("private-device"));
    let status: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(status["ready"], false);
    assert_eq!(status["state"], "degraded");
    assert_eq!(status["restart_attempt"], 0);
    assert_eq!(
        status["session_id"]
            .as_str()
            .expect("worker must write initial session status before setup failure")
            .len(),
        32
    );
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn sigterm_during_restart_backoff_stops_supervisor_cleanly() {
    let (dir, mut value) = fixture();
    value["service"]["max_restarts"] = json!(20);
    value["service"]["restart_backoff_ms"] = json!(3000);
    let path = dir.join("config.json");
    fs::write(&path, value.to_string()).unwrap();
    let mut child = ChildGuard(
        Command::new(env!("CARGO_BIN_EXE_capture-service"))
            .arg("--config")
            .arg(path)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let start = Instant::now();
    loop {
        if fs::read_to_string(dir.join("status.json"))
            .ok()
            .is_some_and(|raw| raw.contains("WORKER_EXITED"))
        {
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(5));
        thread::sleep(Duration::from_millis(25));
    }
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    };
    loop {
        if let Some(exit) = child.try_wait().unwrap() {
            assert!(
                exit.success(),
                "supervisor exit status={exit}; status={:?}",
                fs::read_to_string(dir.join("status.json"))
            );
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(5));
        thread::sleep(Duration::from_millis(25));
    }
    let status: Value =
        serde_json::from_str(&fs::read_to_string(dir.join("status.json")).unwrap()).unwrap();
    assert_eq!(status["state"], "stopped");
    assert_eq!(status["reason"], "SIGNAL");
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn concurrent_fixture_creation_never_reuses_clock_resolution_path() {
    let handles: Vec<_> = (0..32)
        .map(|_| std::thread::spawn(|| fixture().0))
        .collect();
    let dirs: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    let unique: std::collections::HashSet<_> = dirs.iter().collect();
    assert_eq!(unique.len(), 32);
    for dir in dirs {
        fs::remove_dir_all(dir).unwrap();
    }
}
