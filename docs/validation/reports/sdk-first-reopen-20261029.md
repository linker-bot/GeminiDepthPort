# Native SDK first reopening: functional verification, 2026-10-29

This follows merged PR #30. The bundled SDK remains exactly 2.10.6 with the same
library SHA256. No firmware, exposure, synchronization, depth-unit, USBFS budget,
stream profile, quality threshold, startup timeout or retry-budget change was used.

The fixed startup performs fingerprinted USB video bulk-IN endpoint cleanup
before SDK creation. This is a GeminiDepthPort native-service repair, not a claim
that the unmodified vendor library independently repairs every crash scenario.
The actual hardware HALT bit was not measured. The evidence identifies a missing
transport-cleanup boundary; it does not certify a specific firmware defect.

## Controls

- Original official C++ SDK capture reproduced first reopening without frames.
- Claiming/releasing interfaces alone still failed on Gemini 305.
- Adding `libusb_clear_halt` before the first SDK reopen produced frames.
- Final same-image, same-service, same-SDK A/B: with the optional recovery entry
  omitted, one allowed restart failed; with it enabled, that first restart reached
  SDK-ready with zero failed reopen attempts. Both variants retained the original
  30-second startup budget and the same pairing/quality settings.

The negative A/B run is preserved as `ok=false`, not relabeled as a successful
acceptance. Its container was cleaned up and no COMPLETE marker was produced.

## Real-device checks

Gemini 305, Gemini 335L and both Gemini 2 devices were tested one at a time. Each
passed with `retry_budget=1`, `actual_restart_delta=1` and
`failed_reopen_attempts=0`. Gemini 305 and 335L each passed a second cycle. These
were short process-fault tests, not multi-camera scale or soak tests.

Each test checked wrong-session/wrong-snapshot refusal, preparing/active native
ACKs, shared Rust ACK validation, worker SIGKILL, no worker/session reopening
while protected, wrong-credential refusal, credentialed release and a fresh SDK
session/snapshot on the first new worker. All owned test containers were removed.
The final parent-death/input-hardened artifact was separately retested on Gemini
305 and also passed the one-restart requirement. Per-run binary fingerprints and
redacted counts are retained in the [machine record](sdk-first-reopen-20261029.json).
No raw images, serials, namespace bindings, calibration or credentials are public.

A real single-camera management flow also passed preview/confirmation, generation
and up/check, calibration export, quality APIs, strong ACK/snapshot context,
recording interlocks, release/down and historical finalized-summary lookup. The
new generator automatically carried the helper fingerprint from the image
manifest into the immutable capture configuration; no proof was fabricated.

## Offline and packaging checks

Rust 1.85 workspace tests and strict Clippy passed. Python ran 273 tests with two
environment-specific skips; CLI/real-loopback HTTP contracts passed 33 checks.
The Linux supervisor suite passed 13 tests, including helper-before-SDK order,
helper failure preventing SDK access, and persistent protection blocking both.
Injected native operations checked complete-scope acquisition, no clear after a
claim failure, and releasing all acquired interfaces on clear/release failures.

The final image smoke ran without USB or network and passed runtime-file hashes,
helper SHA tamper rejection, malformed serial/argument refusal before USB access,
wrong-parent refusal and actual parent-death SIGKILL propagation. SDK validation
continued to report `hardware_verified=false`.

To activate on a deployment, rebuild the Rust image, reconfirm profiles and
render a new generation. Legacy standalone configurations without `usb_recovery`
retain their old behavior. The official ROS default remains unchanged.

Physical accuracy/hardware synchronization, 30-minute stability, scale,
performance and release acceptance were not performed.
