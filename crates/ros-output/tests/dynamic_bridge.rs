use frame_core::{
    FrameEncoding, FrameLayout, PacketMetadata, PairClock, PairEvidence, PixelFormat,
};
use ros_output::{AuxBinding, Binding, Calibration, Publisher, RosAuxPublisher, StaticTransform};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Instant,
};

fn compile(source: &Path, output: &Path, include: &Path, bad_abi: bool) {
    let result = Command::new("c++")
        .args(if cfg!(target_os = "macos") {
            vec!["-dynamiclib"]
        } else {
            vec!["-shared", "-fPIC"]
        })
        .args(["-std=c++17", "-Wall", "-Wextra", "-Werror"])
        .arg(if bad_abi {
            "-DMOCK_ABI_VERSION=2"
        } else {
            "-DMOCK_ABI_VERSION=1"
        })
        .arg("-I")
        .arg(include)
        .arg(source)
        .arg("-o")
        .arg(output)
        .output()
        .expect("C++ compiler required for native ABI fixture");
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
}
fn binding() -> Binding {
    Binding {
        node_name: "test_probe".into(),
        namespace: "/test".into(),
        image_topic: "/test/image_raw".into(),
        camera_info_topic: "/test/camera_info".into(),
        metadata_topic: "/test/frame_metadata".into(),
        frame_id: "test_optical".into(),
    }
}
fn calibration() -> Calibration {
    Calibration::from_json(r#"{"version":1,"width":2,"height":2,"frame_id":"test_optical","distortion_model":"plumb_bob","k":[1,0,1,0,1,1,0,0,1],"d":[0,0,0,0,0],"r":[1,0,0,0,1,0,0,0,1],"p":[1,0,1,0,0,1,1,0,0,0,1,0]}"#).unwrap()
}
fn packet() -> PacketMetadata {
    PacketMetadata {
        layout: FrameLayout {
            width: 2,
            height: 2,
            stride: 4,
            format: PixelFormat::Yuyv,
        },
        encoding: FrameEncoding::Yuyv,
        sequence: 1,
        device_timestamp_us: Some(10),
        system_timestamp_us: Some(1_700_000_000_000_001),
        source_depth_scale_mm: None,
        output_depth_scale_mm: None,
        normalized: false,
        received_at: Instant::now(),
    }
}

#[test]
fn dynamic_abi_copy_guards_and_cleanup() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let dir = std::env::temp_dir().join(format!("gdp-ros-bridge-test-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let source = root.join("tests/mock_bridge.cpp");
    let include = root.join("../../ros/include");
    let library = dir.join("mock.so");
    let bad_abi = dir.join("bad-abi.so");
    compile(&source, &library, &include, false);
    compile(&source, &bad_abi, &include, true);
    assert!(Publisher::load("relative.so", binding(), calibration()).is_err());
    assert!(Publisher::load(&bad_abi, binding(), calibration()).is_err());
    let events = dir.join("events");
    let child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "mock_child", "--ignored"])
        .env("GDP_ROS_MOCK_LIBRARY", &library)
        .env("GDP_ROS_MOCK_EVENTS", &events)
        .output()
        .unwrap();
    assert!(
        child.status.success(),
        "{}{}",
        String::from_utf8_lossy(&child.stdout),
        String::from_utf8_lossy(&child.stderr)
    );
    let log = fs::read_to_string(events).unwrap();
    assert_eq!(log.lines().filter(|l| *l == "create").count(), 2);
    assert_eq!(log.lines().filter(|l| *l == "destroy").count(), 2); // includes error+non-null handle
    assert_eq!(log.matches("publish-copied\n").count(), 3);
    assert_eq!(log.lines().filter(|l| *l == "publish-failed").count(), 1);
    assert_eq!(log.lines().filter(|l| *l == "aux-create").count(), 2);
    assert_eq!(log.lines().filter(|l| *l == "aux-destroy").count(), 2);
    assert_eq!(log.lines().filter(|l| *l == "aux-publish").count(), 2);
    assert_eq!(
        log.lines().filter(|l| *l == "aux-publish-failed").count(),
        1
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
#[ignore = "executed in isolated subprocess by dynamic_abi_copy_guards_and_cleanup"]
fn mock_child() {
    let library = PathBuf::from(std::env::var_os("GDP_ROS_MOCK_LIBRARY").unwrap());
    let mut failing = binding();
    failing.node_name = "mock_fail_create".into();
    assert!(Publisher::load(&library, failing, calibration()).is_err());
    let mut publisher = Publisher::load(&library, binding(), calibration()).unwrap();
    for invalid in [
        "",
        "short",
        "0123456789abcdef0123456789abcdeG",
        "0123456789abcdef0123456789abcde\"",
    ] {
        assert!(publisher.set_session_id(invalid).is_err());
    }
    publisher
        .set_session_id("0123456789abcdef0123456789abcdef")
        .unwrap();
    assert!(
        publisher
            .set_session_id("fedcba9876543210fedcba9876543210")
            .is_err()
    );
    let mut metadata = packet();
    let stamp = metadata.system_timestamp_us.unwrap() * 1000;
    let mut bytes = vec![16, 128, 32, 128, 64, 90, 80, 240];
    assert!(publisher.publish(&metadata, &bytes[..7], stamp).is_err());
    assert!(publisher.publish(&metadata, &bytes, stamp + 1).is_err());
    // A device clock can start at zero; the independently mapped Unix/system
    // timestamp remains positive. Preserve this zero, do not replace with null.
    metadata.device_timestamp_us = Some(0);
    publisher.publish(&metadata, &bytes, stamp).unwrap();
    bytes.fill(255);
    drop(bytes); // native side must own a copy after call
    assert!(
        publisher
            .publish(&metadata, &[16, 128, 32, 128, 64, 90, 80, 240], stamp)
            .is_err()
    );
    metadata.sequence = 2;
    metadata.system_timestamp_us = Some(stamp / 1000 + 1);
    let bytes = [16, 128, 32, 128, 64, 90, 80, 240];
    publisher.publish(&metadata, &bytes, stamp + 1000).unwrap();
    metadata.sequence = 999;
    metadata.system_timestamp_us = Some(stamp / 1000 + 2);
    assert!(publisher.publish(&metadata, &bytes, stamp + 2000).is_err());
    // Failed C publication cannot advance Rust-side timestamp/sequence guards.
    metadata.sequence = 3;
    publisher.publish(&metadata, &bytes, stamp + 2000).unwrap();
    metadata.sequence = 4;
    metadata.system_timestamp_us = Some(stamp / 1000 + 3);
    metadata.layout.format = PixelFormat::Depth16;
    metadata.encoding = FrameEncoding::Depth16;
    metadata.source_depth_scale_mm = Some(0.1);
    metadata.output_depth_scale_mm = Some(0.1);
    assert!(publisher.publish(&metadata, &bytes, stamp + 3000).is_err());
    drop(publisher);
    let extrinsics = orbbec_safe::Extrinsics::from_sdk(
        [0., -1., 0., 1., 0., 0., 0., 0., 1.],
        [100., 200., 300.],
    )
    .unwrap();
    let transform =
        StaticTransform::from_depth_to_color(&extrinsics, "color_optical", "depth_optical")
            .unwrap();
    let binding = AuxBinding {
        node_name: "mock_fail_aux_create".into(),
        namespace: "/test".into(),
        pair_metadata_topic: "/test/rgbd/pair_metadata".into(),
    };
    assert!(RosAuxPublisher::load(&library, binding, transform.clone(), stamp).is_err());
    let binding = AuxBinding {
        node_name: "aux_probe".into(),
        namespace: "/test".into(),
        pair_metadata_topic: "/test/rgbd/pair_metadata".into(),
    };
    let mut auxiliary = RosAuxPublisher::load(&library, binding, transform, stamp).unwrap();
    let mut evidence = PairEvidence {
        pair_sequence: 1,
        color_sequence: 10,
        depth_sequence: 20,
        color_timestamp_us: 1,
        depth_timestamp_us: 11,
        delta_us: 10,
        clock: PairClock::Device,
        same_frameset: true,
        hardware_sync_verified: false,
    };
    auxiliary
        .set_session_id("0123456789abcdef0123456789abcdef")
        .unwrap();
    assert!(
        auxiliary
            .set_session_id("fedcba9876543210fedcba9876543210")
            .is_err()
    );
    let zero_evidence = PairEvidence {
        color_timestamp_us: 0,
        depth_timestamp_us: 10,
        ..evidence
    };
    assert!(
        auxiliary
            .publish_pair(&zero_evidence, stamp, stamp + 1000)
            .is_err()
    );
    auxiliary
        .publish_pair(&evidence, stamp, stamp + 1000)
        .unwrap();
    assert!(
        auxiliary
            .publish_pair(&evidence, stamp, stamp + 1000)
            .is_err()
    );
    evidence.pair_sequence = 999;
    evidence.color_sequence += 1;
    evidence.depth_sequence += 1;
    evidence.color_timestamp_us += 100;
    evidence.depth_timestamp_us += 100;
    assert!(
        auxiliary
            .publish_pair(&evidence, stamp + 2000, stamp + 3000)
            .is_err()
    );
    evidence.pair_sequence = 2;
    auxiliary
        .publish_pair(&evidence, stamp + 2000, stamp + 3000)
        .unwrap();
    evidence.pair_sequence = 3;
    evidence.color_sequence += 1;
    evidence.depth_sequence += 1;
    // Header stamps advance but selected pairing clock timestamps do not.
    assert!(
        auxiliary
            .publish_pair(&evidence, stamp + 4000, stamp + 5000)
            .is_err()
    );
    let regressed = PairEvidence {
        color_timestamp_us: 100,
        depth_timestamp_us: 110,
        ..evidence
    };
    assert!(
        auxiliary
            .publish_pair(&regressed, stamp + 4000, stamp + 5000)
            .is_err()
    );
    let changed_clock = PairEvidence {
        clock: PairClock::System,
        color_timestamp_us: stamp / 1000 + 4,
        depth_timestamp_us: stamp / 1000 + 5,
        delta_us: 1,
        ..evidence
    };
    assert!(
        auxiliary
            .publish_pair(&changed_clock, stamp + 4000, stamp + 5000)
            .is_err()
    );
    evidence.color_timestamp_us += 100;
    evidence.depth_timestamp_us += 100;
    evidence.hardware_sync_verified = true;
    assert!(
        auxiliary
            .publish_pair(&evidence, stamp + 4000, stamp + 5000)
            .is_err()
    );
    drop(auxiliary);
}
