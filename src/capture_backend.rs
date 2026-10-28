//! Explicit opt-in Rust data plane. Official driver remains the compatibility default.
use crate::{control::Controller, util::*};
use anyhow::Result;
use serde_json::{Value, json};
pub const SDK_PATH: &str = "/opt/orbbec-sdk/lib/libOrbbecSDK.so";
pub const BRIDGE_PATH: &str = "/opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so";
pub const SERVICE_PATH: &str = "/usr/local/bin/capture-service";
pub const SOURCE_COMMIT: &str = "c153462518ad674650bafa4464fda72c27ab797a";
pub fn selected(d: &Value) -> &str {
    d.get("capture_backend")
        .and_then(Value::as_str)
        .unwrap_or("official")
}
/// Preview evidence binds shared configuration for every backend, matching the
/// management confirmation contract introduced by the upstream binding fix.
pub fn preview_revision(d: &Value, camera: &Value) -> String {
    crate::config::camera_digest(d, camera)
}
fn exact(v: &Value, fields: &[&str]) -> bool {
    v.as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|k| m.contains_key(*k)))
}
pub fn validate(d: &Value) -> Result<()> {
    require(
        d.get("capture_backend")
            .is_none_or(|v| v == "official" || v == "rust"),
        "CAPTURE_BACKEND",
        "Unknown capture backend",
    )?;
    if selected(d) == "official" {
        return require(
            d.get("rust_capture").is_none(),
            "CAPTURE_BACKEND",
            "Rust policy requires explicit Rust backend",
        );
    }
    require(
        d["schema_version"] == 2,
        "CAPTURE_BACKEND_SCHEMA",
        "Rust capture requires schema v2",
    )?;
    let p = &d["rust_capture"];
    require(
        exact(
            p,
            &[
                "depth_unit_policy",
                "pairing",
                "poll_ms",
                "capacity",
                "startup_discovery_seconds",
                "service",
            ],
        ),
        "RUST_CAPTURE_POLICY",
        "Explicit complete Rust capture policy required",
    )?;
    require(
        p["depth_unit_policy"] == "require-mm" || p["depth_unit_policy"] == "normalize-mm",
        "RUST_DEPTH_POLICY",
        "Explicit depth unit policy required",
    )?;
    require(
        exact(&p["pairing"], &["clock", "max_delta_us"])
            && (p["pairing"]["clock"] == "device" || p["pairing"]["clock"] == "system")
            && p["pairing"]["max_delta_us"]
                .as_u64()
                .is_some_and(|v| (1..=1_000_000).contains(&v)),
        "RUST_PAIR_POLICY",
        "Explicit pairing clock and tolerance required",
    )?;
    require(
        p["poll_ms"]
            .as_u64()
            .is_some_and(|v| (1..=1000).contains(&v))
            && p["capacity"]
                .as_u64()
                .is_some_and(|v| (1..=1024).contains(&v))
            && p["startup_discovery_seconds"]
                .as_f64()
                .is_some_and(|v| v.is_finite() && (0.0..=30.0).contains(&v)),
        "RUST_CAPTURE_POLICY",
        "Invalid capture limits",
    )?;
    let service = &p["service"];
    let service_keys = [
        "idle_timeout_ms",
        "startup_timeout_ms",
        "heartbeat_ms",
        "heartbeat_timeout_ms",
        "max_restarts",
        "restart_backoff_ms",
        "shutdown_timeout_ms",
        "ready_min_pairs",
    ];
    require(
        exact(service, &service_keys)
            && service_keys.iter().all(|k| service[*k].as_u64().is_some()),
        "RUST_SERVICE_POLICY",
        "Explicit bounded supervisor policy required",
    )?;
    require(
        service["idle_timeout_ms"]
            .as_u64()
            .is_some_and(|v| (100..=60_000).contains(&v))
            && service["startup_timeout_ms"]
                .as_u64()
                .is_some_and(|v| (1000..=120_000).contains(&v))
            && service["heartbeat_ms"]
                .as_u64()
                .is_some_and(|v| (50..=5000).contains(&v))
            && service["heartbeat_timeout_ms"]
                .as_u64()
                .is_some_and(|v| (500..=120_000).contains(&v))
            && service["max_restarts"].as_u64().is_some_and(|v| v <= 20)
            && service["restart_backoff_ms"]
                .as_u64()
                .is_some_and(|v| (100..=30_000).contains(&v))
            && service["shutdown_timeout_ms"]
                .as_u64()
                .is_some_and(|v| (100..=5000).contains(&v))
            && service["ready_min_pairs"]
                .as_u64()
                .is_some_and(|v| (1..=10_000).contains(&v)),
        "RUST_SERVICE_POLICY",
        "Supervisor limits outside supported range",
    )?;
    require(
        service["idle_timeout_ms"].as_u64().unwrap() >= p["poll_ms"].as_u64().unwrap()
            && service["startup_timeout_ms"].as_u64().unwrap()
                > service["idle_timeout_ms"].as_u64().unwrap()
            && service["heartbeat_timeout_ms"].as_u64().unwrap()
                >= service["heartbeat_ms"].as_u64().unwrap().saturating_mul(3)
            && p["startup_discovery_seconds"].as_f64().unwrap() * 1000.0
                < (service["startup_timeout_ms"].as_u64().unwrap() as f64),
        "RUST_SERVICE_POLICY",
        "Supervisor deadlines inconsistent",
    )?;
    let allowed = [
        "enable_color",
        "enable_depth",
        "color_format",
        "depth_format",
        "depth_registration",
        "color_width",
        "color_height",
        "color_fps",
        "depth_width",
        "depth_height",
        "depth_fps",
        "color_qos",
        "color_camera_info_qos",
        "depth_qos",
        "depth_camera_info_qos",
    ];
    for c in d["cameras"].as_array().into_iter().flatten() {
        let parameters = &c["parameters"];
        require(
            parameters
                .as_object()
                .is_some_and(|m| m.keys().all(|k| allowed.contains(&k.as_str()))),
            "RUST_PARAMETER_UNSUPPORTED",
            "Rust backend cannot apply this official-driver parameter",
        )?;
        require(
            parameters["enable_color"] == true && parameters["enable_depth"] == true,
            "RUST_STREAMS",
            "Rust service currently requires both RGB-D streams",
        )?;
        if c["enabled"] == true {
            require(
                c["timestamp_source"] == "host",
                "RUST_TIME_SOURCE",
                "Rust capture requires explicit host/system Unix timestamp source",
            )?;
        }
    }
    Ok(())
}
pub fn runtime(ctl: &Controller, d: &Value) -> Result<Value> {
    let inspect: Value =
        serde_json::from_str(&ctl.run(&["docker", "image", "inspect", text(&d["image"])])?)?;
    let image = &inspect[0];
    require(
        image["Config"]["Labels"]["org.geminidepthport.capture-backend"] == "rust-v1"
            && image["Config"]["Labels"]["org.opencontainers.image.revision"]
                == d["upstream_commit"],
        "RUST_IMAGE_CONTRACT",
        "Rust capture image source/backend differs",
    )?;
    let raw = ctl.run(&[
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "cat",
        text(&d["image"]),
        "/opt/gdp-capture/runtime.json",
    ])?;
    let manifest: Value = serde_json::from_str(&raw)?;
    require(
        manifest["version"] == 1
            && manifest["upstream_commit"] == d["upstream_commit"]
            && manifest["sdk_version"] == "2.10.6"
            && manifest["communication_profile"] == crate::config::COMMUNICATION,
        "RUST_RUNTIME_CONTRACT",
        "Unsupported capture runtime manifest",
    )?;
    for key in ["sdk_sha256", "bridge_sha256", "service_sha256"] {
        require(
            matches(r"^[a-f0-9]{64}$", text(&manifest[key])),
            "RUST_RUNTIME_CONTRACT",
            "Runtime binary hash missing",
        )?;
    }
    require(
        manifest["sdk_source"]["repository"] == "https://github.com/orbbec/OrbbecSDK_ROS2"
            && manifest["sdk_source"]["commit"] == d["upstream_commit"]
            && manifest["sdk_source"]["license_files"]
                .as_object()
                .is_some_and(|m| !m.is_empty())
            && manifest["files"].as_object().is_some_and(|m| !m.is_empty()),
        "RUST_RUNTIME_CONTRACT",
        "Runtime source/license inventory missing",
    )?;
    for (path, key) in [
        (SDK_PATH, "sdk_sha256"),
        (BRIDGE_PATH, "bridge_sha256"),
        (SERVICE_PATH, "service_sha256"),
    ] {
        require(
            manifest["files"][path] == manifest[key],
            "RUST_RUNTIME_CONTRACT",
            "Runtime binary hash does not match file inventory",
        )?;
    }
    require(
        matches(r"^sha256:[a-f0-9]{64}$", text(&image["Id"])),
        "RUST_RUNTIME_CONTRACT",
        "Immutable image identity missing",
    )?;
    Ok(json!({"image_id":image["Id"],"manifest_sha256":hash(raw.as_bytes()),"manifest":manifest}))
}
pub fn runtime_digest(value: &Value) -> String {
    hash(canonical(value))
}
pub fn service_config(d: &Value, c: &Value, generation: &str, runtime: &Value) -> Value {
    let p = &c["parameters"];
    let policy = &d["rust_capture"];
    let stream = |name: &str| json!({"width":p[format!("{name}_width")],"height":p[format!("{name}_height")],"fps":p[format!("{name}_fps")],"frame_id":format!("{}_{name}_optical_frame",text(&c["frame_prefix"]))});
    let mut depth = stream("depth");
    depth["unit_policy"] = policy["depth_unit_policy"].clone();
    let mut service = policy["service"].clone();
    service["status_path"] = "/tmp/capture/status.json".into();
    service["generation"] = generation.into();
    let m = &runtime["manifest"];
    json!({"version":1,"sdk_library":SDK_PATH,"serial":c["serial_number"],"color":stream("color"),"depth":depth,"pairing":policy["pairing"],"poll_ms":policy["poll_ms"],"capacity":policy["capacity"],"startup_discovery_seconds":policy["startup_discovery_seconds"],"ros":{"bridge":BRIDGE_PATH,"namespace":c["namespace"],"stamp_source":"sdk-system-unix"},"service":service,"runtime_contract":{"sdk_sha256":m["sdk_sha256"],"bridge_sha256":m["bridge_sha256"],"service_sha256":m["service_sha256"]}})
}
