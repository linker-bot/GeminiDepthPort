# Capture completion: functional validation, 2026-10-29

This change keeps the production configuration limit at three cameras. More
cameras are permitted only by the isolated test runner. The runner's four-camera
lifecycle evidence in this change is simulated and explicitly marked as such.
No 30-minute soak, scale, performance comparison, release acceptance or physical
accuracy certification was executed in this round.

## Actual device and recording checks

A short single-device Gemini 305 recording captured ten SDK frameset pairs with
native depth scale `0.10000000149011612 mm/count`. The raw recording finished
without stopping ROS publication. Its native/counts and ROS/millimetre snapshots
have separate content IDs and an explicit association. The offline v2 reader
validated all ten records and both payloads. Preserved-count PNG output decoded
pixel-for-pixel equal to the native samples; the TAR members contained exactly
the same PNG bytes. No millimetre conversion was used in this check.

A separate five-second strict ROS consumer run overlapped a 300-pair native
recording and passed RGB-D, frame/pair metadata and late-join static TF checks.
The fixture required 100 pairs for that window; FPS, skew, layout and freshness
limits were unchanged. The late-TF subscription now triggers an immediate QoS
check, with a regression that fails when the fix is removed.

A separate one-byte recording budget failed explicitly, left no finalized output
directory and continued ROS publication. This is a deterministic limit check,
not a full-disk, stress or durability benchmark. Test process cleanup stopped and
removed only the named containers created for these checks.

The independently compiled ROSbag smoke generated real CDR using ROS Humble,
wrote and read an actual sqlite3 bag and then an actual MCAP bag, ran the Rust
observer and offline CLI, and checked exact preservation of original CDR bytes.
Both storage backends passed. The existing test image lacked the MCAP plugin;
the official ROS plugin and vendor package were installed only in a disposable
container, without changing the host installation.

## SDK alignment differences

Each of Gemini 305, Gemini 335L and the two Gemini 2 devices was opened
individually for one selected complete SDK frameset. The source pixels were
saved before official SDK Align and checked unchanged afterwards. Both paths
used the actual aligned output profile as their target grid. The Rust reference
retained its nearest-pixel/color-camera-Z semantics. No exposure synchronization
or physical ground truth was inferred.

[Machine-readable differences](sdk-grid-differences-20261029.json) record valid
coverage separately from errors on common valid pixels. Common-pixel median
absolute differences were 0, 1, 1 and 1 mm; p95 values were 210, 12, 6 and 8 mm.
These are observations, not acceptance thresholds. In particular, Gemini 305
has large boundary differences, and the Gemini 2 SDK output has substantially
more valid pixels than the scalar single-pixel projection. SDK rasterization,
gap filling and reported Z semantics are not assumed equivalent to the reference.
SDK equivalence remains unverified; no filtering, hole filling or relaxed
thresholds were introduced to improve these numbers.

An earlier comparison used original distorted color intrinsics against the
SDK's default undistorted output grid. That comparison is superseded and must
not be cited as a same-grid algorithm result. See the
[grid comparison contract](../../tools/validation/sdk-grid-comparison.md).

All snapshots and reports retain `physical_accuracy_verified=false`. The user
explicitly deferred physical accuracy certification. Raw images, device serials,
calibration coefficients, credentials and private deployment paths are excluded
from these public records.

## Native fault recovery investigation

Two isolated real-device tests (Gemini 305 and 335L) rejected wrong-session and
wrong-snapshot requests, verified preparing/active ACKs using the shared Rust
protocol validator, killed only their owned worker, and observed no worker or
session reopen while protected. With a single allowed restart, both tests
failed after credentialed release: the new SDK pipeline returned no frames for
the configured 30-second startup budget. These runs are failed acceptances,
with no COMPLETE marker; cleanup removed their test containers.

An independent official C++ SDK reproduction confirmed the next-open failure
after SIGKILL, both with and without added container capabilities. After that
failed pipeline was normally destroyed, a third open produced a complete SDK
frameset. This excludes a need to attribute the observed failure to Rust pair
guards or ROS publication; it does not establish the SDK's internal root cause.
No USB reset, camera property change or longer startup timeout was applied.

The native harness therefore retains its one-retry default and can separately
test an explicitly selected existing bounded retry budget. A later successful
retry must not rewrite the earlier failed session or the one-retry result.
The production service already has a bounded retry policy; it must continue to
block all such attempts until recording protection is explicitly released.

With an explicitly selected three-restart budget, both devices recovered after
release on the second new worker session: one failed reopen remained recorded.
The final capture binary containing process-group cleanup was separately
retested on Gemini 335L with the same outcome. Startup remained 30 seconds and
no quality threshold changed. The deterministic Linux supervisor suite passed
all ten tests, including reaping a SIGTERM-ignoring helper before reopening and
leaving an unrelated process group alive; the separate group-ownership check
also passed. The helper cleanup fix is independently justified and is not
claimed as the SDK no-frame issue's root cause.

## Complete management flow

A separate Gemini 335L run used an isolated unconfirmed inventory, a local
functional image with the actual new service artifact and a regenerated runtime
manifest. The real preview API observed images before the confirmation API
created its runtime-bound proof. Formal up/check, calibration export, current/
events/summary HTTP APIs, protected native ACK/snapshot context, wrong-credential
refusal, credentialed release, down and owned instance cleanup all passed. No
positive verification flag, calibration or snapshot was written by the test
fixture. This is a single-camera functional check, not a distribution/release or
three-camera scale acceptance.

## Persistent final summaries and interrupted observers

After adding per-generation/per-camera private evidence mounts, the complete
real management flow was rerun. Normal down preserved the original SDK session
summary and sealed snapshot. The history CLI returned `complete=true`,
`snapshot_verified=true`, `live=false` and `worker_ready=false` after the camera
and container had stopped. An earlier archive from the CLI-argument regression
was also readable using the corrected paired generation/session selectors.
History rejects stale event revisions and never supplies a native ACK or an
active protection claim. Crash/no-snapshot cases remain explicitly incomplete.

Official-observer interruption handling was verified with synthetic core and
real subprocess protocol tests. Driver/boundary/core failure facts are retained
separately from normal stop. Failed/timed-out exchanges cannot reuse delayed
responses as successful finalization. A dead core cannot manufacture a final
quality summary. This is not a real-device acceptance of the rebuilt official
ROS driver image; that production image/release path was not executed here.

Final offline checks include workspace Rust tests and strict Clippy at Rust
1.85, 272 Python checks (two environment-specific skips), 33 CLI/HTTP checks,
ten Linux supervisor checks and the separate owned-group check. The Linux
management lifecycle suite passed six synthetic-Docker checks. Source inputs,
credentials, raw pixels and calibration artifacts remain in private test storage.
