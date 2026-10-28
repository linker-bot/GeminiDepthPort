# USB stream recovery before native SDK startup

The unmodified bundled Orbbec SDK 2.10.6 can report successful color/depth stream
startup but return no frames after its previous process was killed. Normal SDK
stop performs USB endpoint cleanup; SIGKILL skips it. This change repairs the
GeminiDepthPort Rust service's startup boundary, without replacing the SDK or
changing its properties, frame interpretation, timeout or retry policy.

## Evidence and mechanism

On the authorized test machine, merely claiming/releasing the selected camera's
interfaces did not repair the first reopening. The same operation followed by
`libusb_clear_halt` on its bulk-IN video endpoints did. The library call clears
endpoint halt state and resets transport synchronization; this experiment proves
the cleanup is sufficient, not that an endpoint's hardware HALT bit was measured
as set or that a particular firmware defect was identified.

References: [libusb device APIs](https://libusb.sourceforge.io/api-1.0/group__libusb__dev.html),
[SDK normal stop cleanup](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/platform/usb/uvc/ObLibuvcDevicePort.cpp).

## Contract

New Rust images include `/opt/gdp-ros/bin/gdp-usb-recover`. Its SHA256 is included
in the immutable runtime file inventory and an optional `usb_recovery` manifest
entry. The generator copies that entry into capture.json. The service verifies
the helper hash before running it; the snapshot retains this explicit policy.
Legacy standalone configurations without the entry retain their old behavior.
Rebuild the image, reconfirm profiles and render a new generation to enable it.

The helper validates its explicit parent PID and registers Linux PDEATHSIG before
reading input or accessing USB. If the worker dies, the helper cannot continue
independently. A no-device smoke test holds stdin open and proves that killing
the named parent kills the helper with SIGKILL.

The helper receives the exact configured device serial through private stdin,
not argv or logs. It requires exactly one Orbbec vendor device with that USB
serial. It plans only VideoStreaming-class bulk-IN endpoints, claims the whole
scope before clearing any endpoint, and releases every claimed interface on
success or failure. Normal libusb automatic kernel-driver detach/reattach is
limited to those selected interfaces. No USB device/bus reset, alt-setting,
exposure, synchronization, depth-unit or calibration-property write is used.
The physical camera must be exclusively owned by this capture task, as with the
SDK itself; another program using that same camera is not a supported owner.

A new worker performs this preflight **before loading/opening the SDK**, including
on initial startup so an earlier container crash is covered. There is no dummy
SDK pipeline, extra SDK start/stop cycle or failed 30-second startup hidden inside
the worker. Success records only interface/endpoint counts in `status.usb_recovery`.
Failure is terminal for that worker and cannot fall back to an unchecked SDK
start. Public status uses `USB_STREAM_RECOVERY_FAILED`; private diagnostics keep
the bounded helper error code. Existing supervisor startup, heartbeat and group
cleanup deadlines also bound this native helper. No threshold is increased.

The supervisor's persistent recording gate blocks creating a worker, hence both
USB cleanup and SDK access. A second worker-side gate check surrounds preflight;
a new guard or shutdown before SDK startup prevents opening the camera. The
helper never removes a guard or authorizes recording. Recovery happens only
when the existing protection lifecycle permits capture startup.

`--validate-config` checks the helper artifact and reports
`usb_recovery_artifact_verified`; it does not run the helper or access USB.
The image smoke test verifies argument refusal and tamper rejection without a
camera. Linux mock SDK tests prove helper-before-SDK ordering, failure-before-SDK
and recording-gate blocking. The injected native operation tests cover all-scope
claiming, no clears after a claim failure and release on clear/release failures.

Physical accuracy, exposure synchronization, scale/performance and release
acceptance remain outside this change.
