# Single-device native gate functional fault test

This is an explicit, short, destructive **test-container worker** fault injection.
It does not access an existing production service. It opens one operator-selected
camera through a newly started capture-service container only when the separate
`--execute --devices-released` flags are supplied. Do not run it while another
SDK/driver owns that physical device. It neither changes USB/sysfs settings nor
performs a scale/performance/physical-accuracy test.

Build the public protocol checker and management CLI for the validation host:

```sh
cargo build --release -p capture-contract --example gate_protocol
cargo build --release --bin geminidepthport
```

Use a new private subdirectory under an existing mode-0700 owner-only parent:

```sh
sudo python3 tools/validation/native_gate_acceptance.py \
  --base-config /absolute/private/existing-capture.json \
  --image EXPLICIT_LOCAL_SDK_ROS_IMAGE \
  --service-binary /absolute/path/new-capture-service \
  --manager-binary /absolute/path/geminidepthport \
  --gate-program /absolute/path/examples/gate_protocol \
  --root /absolute/private/new-gate-test \
  --ros-domain 79 --recovery-restarts 1 --execute --devices-released
```

The base config remains unchanged. The tool copies the selected service artifact
into a private runtime directory, pins its SHA256 in the copied config, installs
a fresh test generation, disables any optional raw sink, sets the explicitly selected isolated recovery retry budget
and uses `/recording/control.json` with `/tmp/capture/status.json`. The selected
image's SDK/bridge must satisfy the config's existing hashes. This test bypasses
the image entrypoint to execute the explicitly supplied binary; it is **not**
a production-distribution/runtime-manifest acceptance claim.

Only a rootful local Docker daemon is allowed. A fresh opaque label/name owns
one test container. The private recording gate is mounted from an isolated
constructed Controller root. Capture data/serials, raw status, snapshots, gate
requests, ACKs and logs remain in the private root. Stdout is a redacted
machine-readable result with booleans/counters and artifact fingerprints; tokens,
serials, complete sessions, namespace/frame names and private paths are omitted.

The test performs these operations:

1. Waits for actual SDK capture ready, a live worker, successful native publication
   calls and a current snapshot bound to status/session/instance/generation.
   This is not a ROS subscriber delivery test.
2. Writes complete typed gate requests with an incorrect session, then an
   incorrect snapshot, and requires no ACK during each bounded observation.
   The snapshot check requires the gate-contract fix that binds ACK generation
   to `status.snapshot_id`. IDs not exposed in status are sourced from the
   current snapshot; the tool does not claim native independent verification of
   additional calibration/configuration fields that status cannot supply.
3. Writes the real preparing request, validates its ACK, transitions control to
   active, and validates that ACK. The pure Rust `gate_protocol` executable
   directly calls the shared `GateRequest::verify_ack`; old-phase, wrong-session
   and wrong-worker ACKs must fail that same verifier. Supervisor identity is
   additionally compared to the actual worker's `/proc` parent.
4. Rechecks worker PID, process start ticks, config digest, session and snapshot
   inside the owned container, then sends SIGKILL to that worker only. It samples
   container `/proc` and status for at least twice configured restart backoff
   (minimum one second), requiring no new worker process, no new session, a
   live supervisor and an explicit protected-recovery-blocked status.
5. Calls the real management CLI `release-protection` on the isolated constructed
   Controller state. An incorrect credential must fail and retain both guard
   and native control. The correct credential is passed only through stdin;
   release must remove control/ACK/guard, after which the same supervisor must
   produce a fresh SDK-ready worker/session/snapshot with unchanged config and
   an actual restart delta between one and the available configured budget.

`--recovery-restarts` accepts 1–3 and defaults to **1**, preserving the original
single-retry fault-test policy. Choosing 3 is a separately labelled test run; it
does not change production defaults, per-attempt startup/quality thresholds or
supervisor retry rules. In particular, a failed one-retry test remains failed
even if a later three-retry run recovers. Use a new private root for every run;
prior evidence is never overwritten or reclassified.

For post-release recovery, the observation deadline is the sum of each remaining
worker's original `startup_timeout_ms` and `shutdown_timeout_ms`, configured
increasing restart backoffs, and the existing ten-second observation grace.
The initial readiness observation keeps its prior single-startup deadline. Any
startup restart already consumed before protection reduces the available
recovery budget. Reports distinguish selected `retry_budget`, remaining budget,
`actual_restart_delta`, and `failed_reopen_attempts`. On successful restart delta
`d`, `d-1` prior reopened workers failed; on incomplete runs, observed failed
sessions remain counted without labelling an in-progress worker a completed
failure. The original injected worker is not counted as a failed *reopen*.

Every post-release observation, including each new session's opening/failed
state and zero-frameset/published counters, is retained in private
`recovery-observations.jsonl` (64 MiB cap, mode 0600). The status/quality tree and
container logs remain private after cleanup. Private `report.json` includes
evidence paths; stdout omits those paths and all session/serial identifiers.
A later successful session does not erase earlier failure/zero-frame evidence.
Credentialed release and protected-no-reopen assertions are reported as soon as
they succeed, even if hardware subsequently fails every reopen attempt.

The credential recovery receipt is private mode 0600 and supports inspection if
the test fails before release. Failure intentionally retains guard/control facts;
cleanup stops/removes only this test's label-verified container. The recovery
credential and private source inputs are never included in logs or stdout.
`COMPLETE` exists only after every functional assertion and cleanup succeeds.
A failed or interrupted test is not a successful acceptance result.

This covers **real native gate behavior plus credentialed release against a
constructed isolated Controller state**. It does not cover the full management
`protect` orchestration, Docker ownership/generation reconstruction, official
backend observer contexts, authenticated HTTP flow, application episode validity,
ROS end-consumer compatibility, physical USB disconnect or physical accuracy.
`full_management_ownership_context_verified` remains false even after success.

## Hardware-free regression tests

```sh
cargo build -p capture-contract --example gate_protocol
cargo build --bin geminidepthport
python3 -m unittest discover -s tests -p 'test_native_gate_acceptance.py'
```

The tests inject FakeDocker/FakeClock and run the real Rust ACK verifier and real
credentialed release CLI on synthetic state. They open no camera or SDK. Their
reports remain `simulated:true`; they cannot substitute for the separately
operator-run functional test. They also reproduce the old wrong-snapshot ACK
behavior to prove the harness refuses it, and verify that refusing an existing
root cannot overwrite its report or data.
