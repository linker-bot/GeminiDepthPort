use capture_contract::{snapshot::*, store::SnapshotStore, *};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::{PermissionsExt, symlink},
};
fn live() -> CaptureSnapshot {
    let session = SessionId::parse("a".repeat(32)).unwrap();
    let instance = InstanceId::parse("b".repeat(64)).unwrap();
    let model = CameraModel {
        frame_id: "depth".into(),
        width: 640,
        height: 480,
        k: [500., 0., 320., 0., 500., 240., 0., 0., 1.],
        distortion_model: DistortionModel::None,
        distortion_coefficients: vec![],
    };
    let calibration = CalibrationRecord {
        version: 1,
        source: CalibrationSource::SdkActiveProfiles,
        actual_runtime_observed: true,
        source_session_id: Some(session.clone()),
        source_instance_id: Some(instance.clone()),
        observed_unix_ns: Some(1),
        models: BTreeMap::from([("depth".into(), model)]),
        transforms: vec![],
        physical_accuracy_verified: false,
        legacy_source_sha256: None,
    };
    let configuration = json!({"depth_width":640,"depth_height":480});
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
        depth: DepthRepresentation::SdkCounts { scale_mm: 0.1 },
        quality_policy: QualityPolicy {
            version: 1,
            max_source_age_ns: 500_000_000,
            max_future_ns: 100_000_000,
            max_pair_delta_us: 20000,
            min_valid_fraction: 0.,
            max_zero_fraction: 1.,
            action: QualityAction::FailClosed,
        },
    }
}
#[test]
fn content_changes_cannot_reuse_ids_or_binding() {
    let sealed = live().seal().unwrap();
    sealed.require_runtime().unwrap();
    let bytes = sealed.to_bytes().unwrap();
    let mut expected = sealed.content.binding.clone();
    expected.instance_id = InstanceId::parse("d".repeat(64)).unwrap();
    assert!(SnapshotEnvelope::parse(&bytes, &expected).is_err());
    expected = sealed.content.binding.clone();
    expected.session_id = SessionId::parse("d".repeat(32)).unwrap();
    assert!(SnapshotEnvelope::parse(&bytes, &expected).is_err());
    let mut changed = sealed.clone();
    changed.content.configuration["depth_width"] = json!(320);
    assert!(changed.to_bytes().is_err());
    let mut changed = sealed.clone();
    changed
        .content
        .calibration
        .models
        .get_mut("depth")
        .unwrap()
        .k[0] = 501.;
    assert!(changed.to_bytes().is_err());
    let mut changed = sealed.clone();
    changed.snapshot_id = SnapshotId::parse("e".repeat(64)).unwrap();
    assert!(changed.to_bytes().is_err());
}
#[test]
fn store_publishes_private_immutable_content_and_refuses_tampering() {
    let temp = tempfile::tempdir().unwrap();
    let directory = temp.path().join("store");
    let store = SnapshotStore::open(&directory).unwrap();
    let envelope = live().seal().unwrap();
    let id = store.publish(&envelope).unwrap();
    assert_eq!(id, store.publish(&envelope).unwrap());
    assert_eq!(
        store.read(&id, &envelope.content.binding).unwrap(),
        envelope
    );
    let file = directory.join(format!("{id}.json"));
    assert_eq!(
        fs::metadata(&file).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::write(&file, b"{}").unwrap();
    assert!(store.publish(&envelope).is_err());
    assert_eq!(fs::read(file).unwrap(), b"{}");
    assert!(fs::read_dir(directory).unwrap().all(|e| {
        !e.unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".pending")
    }));
}
#[test]
fn store_rejects_symlink_and_world_readable_input() {
    let temp = tempfile::tempdir().unwrap();
    let directory = temp.path().join("store");
    let store = SnapshotStore::open(&directory).unwrap();
    let envelope = live().seal().unwrap();
    let id = store.publish(&envelope).unwrap();
    let file = directory.join(format!("{id}.json"));
    fs::set_permissions(&file, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(store.read(&id, &envelope.content.binding).is_err());
    fs::remove_file(&file).unwrap();
    symlink(temp.path().join("outside"), &file).unwrap();
    assert!(store.publish(&envelope).is_err());
    let link = temp.path().join("store-link");
    symlink(&directory, &link).unwrap();
    assert!(SnapshotStore::open(&link).is_err());
}
#[test]
fn legacy_import_verifies_old_digest_without_creating_runtime_claims() {
    let m = &live().calibration.models["depth"];
    let mut old = json!({"kind":"geminidepthport-calibration-v1","structural_validation":true,"physical_accuracy_verified":false,"cameras":[{"name":"synthetic","intrinsics":{"depth":{"frame_id":m.frame_id,"width":m.width,"height":m.height,"k":m.k,"distortion_model":"plumb_bob","d":[0,0,0,0,0]}}}]});
    let bytes = serde_json::to_vec(&old).unwrap();
    let hash = ring::digest::digest(&ring::digest::SHA256, &bytes)
        .as_ref()
        .iter()
        .flat_map(|b| {
            const HEX: &[u8; 16] = b"0123456789abcdef";
            [
                HEX[(b >> 4) as usize] as char,
                HEX[(b & 15) as usize] as char,
            ]
        })
        .collect::<String>();
    old["snapshot_sha256"] = json!(hash);
    let original = serde_json::to_vec(&old).unwrap();
    let imported = import_legacy(&original, "synthetic").unwrap();
    assert_eq!(original, serde_json::to_vec(&old).unwrap());
    assert!(!imported.actual_runtime_observed);
    assert!(imported.source_session_id.is_none());
    let mut snapshot = live();
    snapshot.binding.calibration_id = imported.id().unwrap();
    snapshot.calibration = imported;
    let envelope = snapshot.seal().unwrap();
    assert!(envelope.require_runtime().is_err());
    let mut corrupt: Value = old;
    corrupt["cameras"][0]["name"] = json!("different");
    assert!(import_legacy(&serde_json::to_vec(&corrupt).unwrap(), "synthetic").is_err());
}
