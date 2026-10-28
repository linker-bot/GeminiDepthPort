# Session-bound recording protection

Rust capture protection is a durable handshake, not a label meaning that frames
were received. It does not certify delivery or physical calibration accuracy.
The official backend retains its existing v1 protection contract.

`capture-snapshot` reads the worker's immutable SDK SnapshotEnvelope, preserving
its native snapshot ID. It never reconstructs a different ROS snapshot for Rust
recording protection. Status must explicitly report the ID and the exact private
path `/tmp/capture/snapshots/<id>.json`. Missing native evidence fails closed.
The reader pins private directory FDs, checks 0700/0600 ownership, regular-file
and single-link properties, size bounds and O_NOFOLLOW. It verifies the complete
content hash, active-SDK calibration provenance, session, supervisor instance,
generation, actual capture.json SHA256, native geometry, D2C transform and
millimetre depth representation. Container/worker identity and snapshot
reference must remain the same before and after reading every snapshot.

The existing official-backend `calibration-export` continues to collect observed
ROS CameraInfo/TF. That provenance legitimately has a different ID and cannot be
substituted for a Rust worker's SDK evidence. Legacy imports alone cannot
authorize recording. This stage's zero/one pixel fractions only declare
report-only behavior; no business validity threshold or image accuracy
certification is inferred.

Each Rust generation creates an owner-only `state/recording-gates/<camera>`
directory and mounts it read/write at `/recording`. Immutable `/config` remains
read-only. The service configuration explicitly names `/recording/control.json`.
Old service configs can still run, but cannot claim the new protection contract
without its persistent control mount.

`protect` collects session-bound snapshots, persists a v2 `preparing` guard, and
installs preparing requests for all cameras. The native supervisor acknowledges
only a request matching its current ready worker, generation, config hash,
supervisor instance and session. Management rechecks snapshots, actual ROS
health and live identities before advancing gates and then the guard to `active`.
The immutable request digest binds each ACK to its request phase and nonce.
Any failure after preparing retains the guard and installed controls. It never
returns an active recording claim or erases failed preparation automatically.

`GDP_CAPTURE_INSTANCE_ID` is a 64-hex ID generated once per supervisor lifetime
and passed explicitly to worker children. Status reports it as `instance_id`.
Worker restarts retain that instance and receive a new 32-hex session. Management
also independently tracks Docker container identity; a change to either layer
invalidates a live witness. Rust snapshot acquisition requires an explicit native `instance_id`; a legacy
status missing native instance or snapshot evidence cannot authorize this gate.

Any recording control present at the persistent path blocks automatic native
worker recovery, including unreadable, malformed or symlink controls. A failed
worker is bounded and reaped, but the SDK is not reopened while a gate remains.
Status becomes degraded with `RECORDING_PROTECTED_RECOVERY_BLOCKED`. An existing
control also prevents initial device open after supervisor/container restart.
A valid request is ACKed once, rather than fsyncing on every heartbeat.

`recording-status` is a machine-readable, token-free view of unprotected,
preparing, active, releasing or invalid protection. A malformed guard remains
blocked. On a failed prepare, the owner-only
`state/recording-recovery-credential` retains the credential even if CLI could
not return it. Keep this file private. Supply its contents on stdin to
`release-protection`; credentials never belong in command arguments or logs.

Authenticated release validates the full deterministic camera scope first,
persists `releasing`, removes only its private control/ACK files, fsyncs, and
then removes the guard and recovery receipt. It works when services are offline
and when control files became malformed. No Docker access is required to release
these gates. Wrong credentials and invalid scope leave protection in place.
Removing a valid gate is explicit authorization for a waiting supervisor to
resume its configured recovery policy; it is not a continuation of the protected
recording session. Consumers must observe a new session separately.

Tests use synthetic geometry, fake Docker, temporary private stores and native
ABI fixtures. Linux supervision tests stop a mock SDK worker, require ACK and
recovery blocking, verify no second SDK open while protected, and permit a new
session only after explicit gate removal. No test establishes real-device or
physical calibration accuracy.
