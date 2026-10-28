# RGB-D functional validation — 2026-10-29

Scope: native recording, protection/recovery, managed evidence and offline readers.
Production configuration remains limited to three cameras; the four-camera lifecycle
fixture in this report is simulated. This report contains no final-release acceptance,
new 30-minute soak, performance comparison or physical accuracy certification.
The [machine-readable record](capture-completion-functional-20261029.json) retains the
result scope. Raw images, device identities, credentials and calibration remain private.

## Native recording and ROS delivery

A real Gemini 305 captured ten native SDK frameset pairs at
`0.10000000149011612 mm/count`. Recording completed while ROS publication continued.
Native/counts and ROS/millimetre snapshots have distinct content IDs and an explicit
association. The v2 reader validated all records and payloads. Preserved-count PNG
pixels matched the native samples; TAR contained exactly the same PNG bytes.

A five-second strict ROS consumer overlapped a 300-pair native recording and passed
RGB-D, frame/pair metadata and late-join static TF checks. The window required 100 pairs;
FPS, skew, layout and freshness limits were unchanged. A one-byte recording budget
failed without finalizing output and left ROS publication running. This is a limit
check, not a full-disk or durability test.

Actual ROS Humble CDR was written and read through sqlite3 and MCAP bags, the Rust
observer and offline CLI. Both storage backends preserved the original CDR bytes.

## SDK alignment comparison

Gemini 305, Gemini 335L and two Gemini 2 devices each supplied one complete frameset.
Source pixels were preserved before SDK Align and unchanged afterwards. Both methods
used the SDK's actual aligned output grid. The Rust reference used nearest-pixel,
color-camera-Z projection; no exposure synchronization or physical ground truth was inferred.

[Differences](sdk-grid-differences-20261029.json) distinguish coverage from error on
common valid pixels. Median absolute differences were 0, 1, 1 and 1 mm; p95 values
were 210, 12, 6 and 8 mm. These are observations, not acceptance thresholds. Gemini 305
boundary differences and the greater SDK valid coverage on Gemini 2 remain unexplained
algorithm differences. Rasterization, filling and Z semantics are not assumed equivalent.

Comparisons using distorted color intrinsics against the SDK's undistorted output grid
are invalid for same-grid conclusions. The [comparison protocol](../../../tools/validation/sdk-grid-comparison.md)
defines the correct grid. All snapshots retain `physical_accuracy_verified=false`.

## Protected fault recovery

Real-device tests on Gemini 305 and 335L rejected wrong-session/snapshot requests,
verified preparing/active ACKs and killed only the owned worker. No reopen occurred
while protected. With one allowed restart, both devices failed to produce frames
within the unchanged 30-second startup budget after credentialed release. These are
failed acceptances with no COMPLETE marker.

An independent official C++ SDK control reproduced the next-open failure after SIGKILL,
with and without additional container capabilities. After normal destruction of that
failed pipeline, a third open produced a complete frameset. This does not establish the
SDK's internal cause. No USB reset, property change or longer timeout was used.

With an explicitly selected three-restart budget, both devices recovered on the second
new worker session; the failed reopen remained recorded. The final process-group-cleanup
binary was separately retested on 335L with the same result. Ten deterministic Linux
supervisor checks and a separate group-ownership check passed, including cleanup of a
TERM-ignoring helper while leaving an unrelated group alive. Helper cleanup is not
claimed as the SDK no-frame issue's cause.

## Management and persistent quality evidence

A real single-camera 335L run started from an unconfirmed inventory and used actual
preview images to create runtime-bound confirmation. Formal up/check, calibration export,
current/events/summary APIs, native protection ACK/snapshot context, wrong-credential
refusal, authenticated release, down and owned-instance cleanup passed. No positive
confirmation flag, snapshot or calibration was synthesized by the fixture.

After normal down, private generation/camera evidence mounts retained the original
session summary and sealed snapshot. Historical queries reported `complete=true`,
`snapshot_verified=true`, `live=false` and `worker_ready=false`. Stale event revisions
were rejected. History never supplied an active native ACK or protection claim;
crash/no-snapshot cases remained incomplete.

Official-observer interruption behavior passed synthetic core and real subprocess
protocol checks. Driver, boundary and core failures stayed distinct from normal stop;
delayed replies could not finalize a failed exchange, and a dead core could not fabricate
a summary. The rebuilt official ROS driver image was not hardware-qualified in this report.

## Offline checks and limits

Rust 1.85 workspace tests and strict Clippy passed, as did 272 Python checks (two
specific-environment skips), 33 CLI/HTTP checks, ten Linux supervisor checks, six synthetic
management checks and the owned-group check. These counts describe the recorded run,
not every later commit. Physical accuracy, production-scale long-term stability, USB
hotplug and end-consumer compatibility require separate acceptance.
