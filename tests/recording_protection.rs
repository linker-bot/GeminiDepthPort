//! Offline authenticated recovery: no Docker, ROS, SDK or devices are used.
use capture_contract::{recording::*, snapshot::SnapshotBinding, *};
use geminidepthport::{control::Controller, recording, util::*};
use serde_json::json;
use std::fs;
fn prepared(root: &std::path::Path) -> (Controller, String, std::path::PathBuf) {
    let ctl = Controller::new(root).unwrap();
    let token = "synthetic-private-credential".to_string();
    let gate = root.join("state/recording-gates/camera_a");
    private_dir(&gate).unwrap();
    let request = GateRequest {
        version: 1,
        phase: GatePhase::Preparing,
        request_id: SessionId::parse("a".repeat(32)).unwrap(),
        binding: SnapshotBinding {
            instance_id: InstanceId::parse("b".repeat(64)).unwrap(),
            session_id: SessionId::parse("c".repeat(32)).unwrap(),
            generation_id: GenerationId::parse("d".repeat(32)).unwrap(),
            configuration_id: ConfigurationId::parse("e".repeat(64)).unwrap(),
            calibration_id: CalibrationId::parse("f".repeat(64)).unwrap(),
        },
        snapshot_id: SnapshotId::parse("0".repeat(64)).unwrap(),
        runtime_config_sha256: ConfigurationId::parse("1".repeat(64)).unwrap(),
        token_sha256: ConfigurationId::parse(hash(&token)).unwrap(),
    };
    atomic_json(&root.join("state/recording-guard.json"),&json!({"version":2,"state":"preparing","generation":"d".repeat(32),"token_sha256":hash(&token),"requests":[{"camera_name":"camera_a","worker_pid":42,"request":request}]})).unwrap();
    atomic(
        &root.join("state/recording-recovery-credential"),
        token.as_bytes(),
    )
    .unwrap();
    fs::write(gate.join("control.json"), b"corrupt-after-crash").unwrap();
    fs::write(gate.join("ack.json"), b"stale").unwrap();
    (ctl, token, gate)
}
#[test]
fn preparing_failure_stays_protected_and_wrong_credential_cannot_release() {
    let root = tempfile::tempdir().unwrap();
    let (ctl, token, gate) = prepared(root.path());
    assert!(ctl.unprotected().is_err());
    assert!(ctl.release("wrong").is_err());
    assert!(gate.join("control.json").exists());
    let status = recording::status(&ctl);
    assert_eq!(status["blocked"], true);
    assert_eq!(status["state"], "preparing");
    assert!(!status.to_string().contains(&token));
    assert!(!status.to_string().contains(&hash(&token)));
}
#[test]
fn credentialed_release_works_with_offline_service_and_corrupted_gate() {
    let root = tempfile::tempdir().unwrap();
    let (ctl, token, gate) = prepared(root.path());
    ctl.release(&token).unwrap();
    assert!(!gate.join("control.json").exists());
    assert!(!gate.join("ack.json").exists());
    assert!(!root.path().join("state/recording-guard.json").exists());
    assert!(
        !root
            .path()
            .join("state/recording-recovery-credential")
            .exists()
    );
    assert!(ctl.unprotected().is_ok());
    assert_eq!(recording::status(&ctl)["state"], "unprotected");
}
#[test]
fn release_validates_complete_scope_before_removing_any_gate() {
    let root = tempfile::tempdir().unwrap();
    let (ctl, token, gate) = prepared(root.path());
    let path = root.path().join("state/recording-guard.json");
    let mut record = read_json(&path).unwrap();
    let mut second = record["requests"][0].clone();
    second["camera_name"] = "../outside".into();
    record["requests"].as_array_mut().unwrap().push(second);
    atomic_json(&path, &record).unwrap();
    assert!(ctl.release(&token).is_err());
    assert!(gate.join("control.json").exists());
    assert!(path.exists());
}
#[test]
fn malformed_guard_cannot_be_mistaken_for_unprotected() {
    let root = tempfile::tempdir().unwrap();
    let (ctl, _, _) = prepared(root.path());
    fs::write(root.path().join("state/recording-guard.json"), b"malformed").unwrap();
    assert!(ctl.unprotected().is_err());
    assert_eq!(recording::status(&ctl)["state"], "invalid");
}

#[test]
fn legacy_calibration_import_keeps_python_float_and_unicode_hash_contract() {
    let mut old = json!({"kind":"geminidepthport-calibration-v1","structural_validation":true,"physical_accuracy_verified":false,"calibration_revision":"合成😀","cameras":[{"name":"camera_a","intrinsics":{"depth":{"frame_id":"depth","width":640,"height":480,"k":[500.0,0.0,320.0,0.0,500.0,240.0,0.0,0.0,1.0],"distortion_model":"plumb_bob","d":[1e-7,0.0,0.0,0.0,0.0]}}}]});
    old["snapshot_sha256"] = hash(canonical(&old)).into();
    let bytes = serde_json::to_vec(&old).unwrap();
    let converted = capture_contract::snapshot::import_legacy(&bytes, "camera_a").unwrap();
    assert_eq!(converted.models["depth"].distortion_coefficients[0], 1e-7);
    assert!(!converted.actual_runtime_observed);
    assert_eq!(converted.legacy_source_sha256, Some(hash(&bytes)));
}
