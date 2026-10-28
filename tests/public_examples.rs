//! Public onboarding fixtures are validated by the production parser and CLI.
use geminidepthport::config;
use serde_json::{Value, json};
use std::{path::Path, process::Command};

const EXAMPLES: &[(&str, &str, usize)] = &[
    ("inventory.v2.example.json", "official", 3),
    (
        "examples/official-three-camera/inventory.json",
        "official",
        3,
    ),
    ("examples/rust-single-camera/inventory.json", "rust", 1),
];

fn read(path: &str) -> Value {
    serde_json::from_slice(
        &std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join(path)).unwrap(),
    )
    .unwrap()
}

fn rejects(data: &Value, code: &str) {
    let error = config::validate(data, true).unwrap_err().to_string();
    assert!(error.starts_with(code), "expected {code}, received {error}");
}

#[test]
fn public_inventories_are_valid_unbound_drafts_and_cannot_start() {
    for (path, backend, count) in EXAMPLES {
        let data = read(path);
        config::validate(&data, true).unwrap();
        assert_eq!(geminidepthport::capture_backend::selected(&data), *backend);
        assert_eq!(data["cameras"].as_array().unwrap().len(), *count);
        for camera in data["cameras"].as_array().unwrap() {
            assert_eq!(camera["serial_number"], "");
            assert_eq!(camera["enabled"], false);
            assert_eq!(camera["profiles_verified"], false);
            assert!(camera["verification"].is_null());
            assert_eq!(camera["calibration_revision"], "");
        }
        assert!(
            config::validate(&data, false)
                .unwrap_err()
                .to_string()
                .starts_with("NO_ACTIVE_CAMERAS")
        );

        let private_root = tempfile::tempdir().unwrap();
        let base = Path::new(env!("CARGO_MANIFEST_DIR")).join(path);
        let output = Command::new(env!("CARGO_BIN_EXE_geminidepthport"))
            .args([
                "--root",
                private_root.path().to_str().unwrap(),
                "validate",
                "--file",
            ])
            .arg(&base)
            .arg("--draft")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{path}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["valid"], true);
        assert_eq!(report["enabled_count"], 0);
        assert_eq!(report["hardware_verified"], false);
        let output = Command::new(env!("CARGO_BIN_EXE_geminidepthport"))
            .args([
                "--root",
                private_root.path().to_str().unwrap(),
                "validate",
                "--file",
            ])
            .arg(&base)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("NO_ACTIVE_CAMERAS"));
    }
}

#[test]
fn copying_a_draft_cannot_bypass_binding_or_confirmation() {
    for (path, _, _) in EXAMPLES {
        let draft = read(path);
        let mut enabled = draft.clone();
        enabled["cameras"][0]["enabled"] = true.into();
        rejects(&enabled, "CAMERA_UNBOUND");
        let mut fabricated = draft.clone();
        fabricated["cameras"][0]["verification"] = json!({"method":"runtime-probe"});
        rejects(&fabricated, "CONFIRMATION_STALE");
        fabricated["cameras"][0]["profiles_verified"] = true.into();
        rejects(&fabricated, "CONFIRMATION_STALE");
        let mut transformed = draft.clone();
        transformed["cameras"][0]["parameters"]["depth_registration"] = true.into();
        rejects(&transformed, "DEPTH_FORMAT");
    }
}
