# Capture contract v1

`capture-contract` has no acquisition, ROS, Docker or frame-core dependency. Its
public DTOs use snake_case fields and kebab-case enum values. Parse untrusted raw
JSON with `parse_strict` before typed deserialization, then call each DTO's
`validate`. This preserves duplicate-key rejection and rejects nonfinite geometry
before serialization can turn a float into JSON null. Do not construct a
`serde_json::Value` from unchecked floating-point measurements before validation.

`SessionId` and the legacy `GenerationId` are 32 lowercase hexadecimal digits.
`ConfigurationId`, `CalibrationId`, and `SnapshotId` are 64. Constructors and
Serde reject invalid identifiers. New content IDs use domain-separated SHA256
of sorted, compact JSON, rejecting the corresponding ID in its own preimage;
this is a new contract, not a replacement for existing generation hashes.

`FrameContext` binds a frame to session, generation, configuration, calibration,
and snapshot. `sequence`, `device_timestamp_us`, and `system_timestamp_us` are
optional: null means unobserved and observed device zero remains valid. Receive
monotonic time is a nanosecond offset from the named session origin; it is not a
Unix timestamp and cannot be subtracted across sessions. No timestamp or
sequence is manufactured from another clock or arrival order.

Depth is either SDK counts with a positive finite millimetre scale, or integer
millimetres with an explicit quantization policy. The existing conversion rounds
nearest with halves upwards, preserves invalid raw zero, raises positive values
rounding below one to one, and rejects overflow of the entire frame. Neither
representation certifies physical accuracy.

`CameraModel` declares pixel dimensions, row-major K, and exact supported
coefficient ordering. `RigidTransform` always states source/target frames and
`p_target_m = R_source_to_target * p_source_m + translation_m`. Rotation is
row-major, translation is metres, and reflections, nonorthogonal matrices and
nonfinite values are rejected. Units are not inferred from device names.

`QualityPolicy` only declares thresholds and fail-closed/report-only intent.
Evaluation and stable event semantics belong to the subsequent quality core.
`ContractError` carries a v1 class and stable code without private inputs.

## Immutable CaptureSnapshot

The `snapshot` module seals configuration, active calibration, depth
representation and quality policy into one content-addressed envelope. Its
`SnapshotBinding` names the exact 64-hex runtime `InstanceId`, session,
generation, configuration and calibration. `SnapshotEnvelope::parse` checks
strict input JSON, the entire content hash and the caller's expected binding.
Changing either calibration or configuration requires a new ID and envelope.

Runtime calibration provenance explicitly distinguishes `sdk-active-profiles`
from `ros-camera-info-tf`. Both require the observation's session, instance and
Unix observation time, and cannot claim physical accuracy. The adapter acquiring
these observations must recheck that the same live instance/session remains
before publishing the snapshot; the content contract cannot independently prove
that an SDK/ROS observation happened. `legacy-import` only verifies and converts
old calibration bytes. It leaves those bytes untouched, retains their digest,
and cannot satisfy `require_runtime` or authorize recording.

On Unix, `SnapshotStore` pins a private directory FD, creates 0600 files in a
0700 owner-only directory, and uses `openat(O_NOFOLLOW)` and `linkat` to publish
without replacing an existing address. The file and directory are fsynced.
Readers check file ownership, permissions, type, size, link count, filename,
content and binding. Partial temporary files are never addressable snapshots;
a crash may leave an owner-only `.pending-*` file. A corrupt existing address is
an integrity error, never silently overwritten or repaired.
