use orbbec_safe::{Format, Sdk, VideoProfile};
use std::{fs, path::PathBuf, process::Command};

#[test]
fn acquisition_does_not_require_optional_calibration_symbols() {
    let dir = std::env::temp_dir().join(format!("gdp-sdk-optional-test-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let library = dir.join("minimal-sdk.so");
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/mock_sdk.c");
    let output = Command::new("cc")
        .args(if cfg!(target_os = "macos") {
            vec!["-dynamiclib"]
        } else {
            vec!["-shared", "-fPIC"]
        })
        .args([
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DMOCK_NO_CALIBRATION",
        ])
        .arg(source)
        .arg("-o")
        .arg(&library)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let sdk = Sdk::load(&library).unwrap();
    let p = VideoProfile {
        width: 2,
        height: 2,
        fps: 30,
    };
    let mut pipeline = sdk.open("test-device", Some(p), Some(p)).unwrap();
    assert!(pipeline.calibration().is_err());
    let frames = pipeline.wait(10).unwrap().unwrap();
    assert!(frames.calibration().is_err());
    assert_eq!(frames.color().unwrap().unwrap().data().unwrap().len(), 8);
    drop(frames);
    pipeline.stop().unwrap();
    drop(pipeline);
    drop(sdk);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn dynamic_mock_checks_ownership_timeout_and_failure_cleanup() {
    let dir = std::env::temp_dir().join(format!("geminidepthport-sdk-test-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let library = dir.join(if cfg!(target_os = "macos") {
        "mock.dylib"
    } else {
        "mock.so"
    });
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/mock_sdk.c");
    let output = Command::new("cc")
        .args(if cfg!(target_os = "macos") {
            vec!["-dynamiclib"]
        } else {
            vec!["-shared", "-fPIC"]
        })
        .args(["-std=c11", "-Wall", "-Wextra", "-Werror"])
        .arg(source)
        .arg("-o")
        .arg(&library)
        .output()
        .expect("C compiler required for ABI fixture");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    // Run the ownership scenario in a child, so mock environment and state are
    // isolated from Rust's parallel test runner. C fixture aborts on wrong order.
    let events = dir.join("events");
    let child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "mock_child", "--ignored"])
        .env("GEMINIDEPTHPORT_MOCK_LIBRARY", &library)
        .env("GEMINIDEPTHPORT_MOCK_EVENTS", &events)
        .output()
        .unwrap();
    assert!(
        child.status.success(),
        "{}{}",
        String::from_utf8_lossy(&child.stdout),
        String::from_utf8_lossy(&child.stderr)
    );
    let log = fs::read_to_string(events).unwrap();
    assert!(log.contains("frame-add-ref\n"));
    assert_eq!(log.matches("start\n").count(), 1);
    assert_eq!(log.matches("stop\n").count(), 1);
    assert_eq!(log.matches("error-delete\n").count(), 2);
    assert_eq!(log.matches("context-delete\n").count(), 3);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
#[ignore = "invoked in isolated child by dynamic_mock_checks_ownership_timeout_and_failure_cleanup"]
fn mock_child() {
    let sdk = Sdk::load(std::env::var_os("GEMINIDEPTHPORT_MOCK_LIBRARY").unwrap()).unwrap();
    assert_eq!(sdk.version(), 20903);
    let p = VideoProfile {
        width: 2,
        height: 2,
        fps: 30,
    };
    assert!(sdk.open("", Some(p), None).is_err());
    assert!(sdk.open("test-device", None, None).is_err());
    assert!(sdk.open("missing-device", Some(p), None).is_err());
    assert!(
        sdk.open("test-device", Some(VideoProfile { fps: 1, ..p }), None)
            .is_err()
    );
    let mut pipe = sdk.open("test-device", Some(p), Some(p)).unwrap();
    assert!(pipe.wait(0).is_err());
    assert!(pipe.wait(1).unwrap().is_none());
    let frames = pipe.wait(10).unwrap().unwrap();
    let calibration = frames.calibration().unwrap();
    let color = calibration.color.as_ref().unwrap();
    assert_eq!(color.profile, p);
    assert_eq!(color.fx, 1.);
    assert_eq!(color.fy, 2.);
    assert_eq!(color.distortion_model, "plumb_bob");
    assert_eq!(calibration.depth.as_ref().unwrap().format, Format::Depth16);
    assert_eq!(
        calibration.depth_to_color.as_ref().unwrap().translation_m,
        [0.1, 0.2, 0.3]
    );
    assert_eq!(
        calibration
            .depth_to_color
            .as_ref()
            .unwrap()
            .transform_depth_to_color([1., 2., 3.]),
        [-1.9, 1.2, 3.3]
    );
    assert!(pipe.calibration().unwrap().depth_to_color.is_some());
    let frame = frames.color().unwrap().unwrap();
    let cloned = frame.try_clone().unwrap();
    let metadata = frame.metadata().unwrap();
    assert_eq!(metadata.format, Format::Yuyv);
    assert_eq!(metadata.index, 42);
    assert_eq!(metadata.data_size, 8);
    let depth = frames.depth().unwrap().unwrap();
    assert_eq!(depth.metadata().unwrap().depth_scale_mm, Some(0.1));
    drop(depth);
    drop(frames);
    drop(frame);
    pipe.stop().unwrap();
    assert!(pipe.wait(10).is_err());
    drop(pipe);
    drop(sdk);
    assert_eq!(cloned.data().unwrap(), [16, 128, 32, 128, 64, 90, 80, 240]);
    drop(cloned); // last reference finally deletes pipeline and parent objects
}
