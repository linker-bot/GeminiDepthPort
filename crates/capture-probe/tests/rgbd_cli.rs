use std::process::Command;
#[test]
fn rgbd_requires_absolute_private_config_path() {
    for arguments in [
        vec![],
        vec!["--config", "relative.json"],
        vec!["--serial", "private-device"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_rgbd-probe"))
            .args(arguments)
            .output()
            .unwrap();
        assert!(!output.status.success());
        let text = String::from_utf8_lossy(&output.stderr);
        assert!(!text.contains("private-device"));
        assert!(text.contains("RGBD_PROBE_FAILED"));
    }
}
