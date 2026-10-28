# capture-probe

Finite, single-stream acquisition experiment with optional ROS publication. It does not pair RGB-D
frames, verify physical calibration accuracy, or establish exposure synchronization. The drop-oldest
queue is experimental and is not a recording durability guarantee.

Offline exercise (no SDK library or camera access):

```sh
cargo run -p capture-probe -- --synthetic --stream color --width 640 --height 480 --fps 30 --frames 100 --capacity 4 --drain-every 10
```

Synthetic frames are ownership tokens with validated geometry, not real images;
their timings do not predict camera performance. Frame count and bounded queue
accounting are deterministic; monotonic timing samples are not.

Linux hardware exercise, with the official driver stopped for this device:

```sh
cargo run --release -p capture-probe -- --sdk-library /absolute/path/libOrbbecSDK.so --serial ACTUAL_SERIAL --stream color --width 640 --height 480 --fps 30 --seconds 5 --poll-ms 100 --capacity 4
```

Use a profile verified on the actual device. There is no first-device or profile
fallback. `--stream depth` selects Depth16. The default `--depth-unit-policy require-mm`
accepts only a reported scale of 1 millimeter per raw unit. Other scales are
rejected rather than relabeled as millimeter `16UC1`.

To explicitly convert actual depth pixels, use `--depth-unit-policy normalize-mm`
with live depth mode. It validates format/geometry/payload first, multiplies each
native little-endian u16 sample by the actual SDK scale in millimeters, rounds
half upward, preserves invalid zero, and clamps nonzero values rounded below 1 mm
to 1 mm (counted). Overflow rejects the whole frame. Output is a newly allocated,
owned packed little-endian u16 millimeter buffer, including when the input scale
is already 1 mm. The queue holds and consumes that buffer, not only a metadata
claim. This incurs an actual copy and conversion cost; it is not zero-copy or a
performance improvement claim. Color and token-only synthetic mode reject this
nondefault policy. No device depth-unit property is modified.

The live queue holds SDK frame references for color/strict depth or owned pixel buffers for normalized depth, on the acquisition thread. Default
`--drain-every 1` releases each frame promptly. A larger value deliberately retains
frames to exercise bounded backlog and can stress SDK buffers; it is not a
throughput benchmark or a recommended production setting. Processing means validation, optional explicit normalization, and dequeue/release, not downstream image computation.

`queue_wait_ns.p99` measures only host-monotonic enqueue-to-dequeue delay, using up
to the first one million samples. It excludes device, USB and SDK-internal delay.
SDK timestamp fields are raw, unmapped microseconds and are not end-to-end
latency. Live duration is bounded, with at most one final polling-call timeout
and SDK shutdown overhead beyond the requested duration. No received valid frame,
any rejected layout/format/scale/normalization overflow, or an SDK error causes a nonzero exit.

The tool does not print the serial or library path. Native SDK logging is outside
the tool's JSON reporting policy; keep hardware execution logs private.

Failure diagnostics include per-reason frame counters and the first observed
format, dimensions, payload size and SDK depth-scale factor. A frame can increment
multiple reason counters. `depth_semantics.requested_unit` describes the contract;
`depth_unit` is populated only for non-synthetic depth with validated frames and
no contract rejection. SDK scale metadata is not a physical-distance calibration.

Progression counters compare adjacent observed selected-stream frames, including
rejected frames. Timestamp gaps mean a forward jump exceeding 1.5 nominal frame
periods in the raw SDK timestamp fields; timestamp clock mapping remains unproven.
SDK index gaps are reported as gap events and skipped index values, not certified
lost frames. Active-window FPS uses `(observed frames - 1) / (last host arrival -
first host arrival)`, excludes startup/idle time outside that window, and can
include rejected frames. It does not certify requested FPS compliance.

Normalization metrics count successfully converted frames/pixels and their zero
and sub-millimeter clamp samples; failed frames do not contribute successful
pixel statistics. Source SDK scale min/max remain separate from output millimeter
units. `normalization.wall_ns` is host-monotonic conversion/output-layout-validation
wall time, separate from queue residence; it is not process CPU time. All live
SDK depth modes currently require a little-endian host, reflecting the supported
SDK target rather than guessing byte order from pixel contents.

## Optional ROS output

The default probe has no ROS output. To enable it, supply all five options:

```sh
--ros-bridge /absolute/path/libgeminidepthport_ros_bridge.so \
--ros-namespace /experiment_camera \
--ros-frame-id experiment_depth_optical_frame \
--ros-calibration /absolute/private/calibration.json \
--ros-stamp-source sdk-system-unix
```

The calibration file must describe the actual selected stream dimensions and
optical frame, with schema `{version:1,width,height,frame_id,distortion_model,k,d,r,p}`.
There is no fabricated intrinsics fallback. The synchronous bridge publishes
`NAMESPACE/STREAM/image_raw`, `NAMESPACE/STREAM/camera_info`, and
`NAMESPACE/STREAM/frame_metadata`. Metadata and bytes travel through the queue in
one `FramePacket`; copied normalized depth retains the original SDK sequence,
SDK timestamps, source depth scale, output scale 1 mm, and normalization flag.
Queue eviction removes the whole packet together.

`--ros-stamp-source sdk-system-unix` explicitly asserts that the SDK **system**
timestamp is Unix microseconds. It never substitutes the device clock. Missing,
zero, out-of-ROS-range, duplicate or regressing mapped stamps fail publication.
The stamp guard advances only after successful publication. This is an explicit
mapping choice, not evidence of physical exposure time or synchronization.

Queue time is recorded before synchronous ROS publication; bridge publish wall
time is separate. Successful publish calls do not establish subscriber delivery,
frame pairing, TF correctness or recording durability. The bridge owns a ROS
message copy before return, so ROS output adds a copy and is not zero-copy.
Synthetic ownership tokens cannot enable ROS output; a separate real-pixel smoke
test must be used for ROS integration. Failure releases remaining queued packets
without attempting later publication from the failed batch. Diagnostics do not
print the private calibration path.

## Single-Pipeline RGB-D probe

`rgbd-probe` is a separate binary. Existing `cargo run -p capture-probe` still
selects the single-stream binary. Run RGB-D with a private absolute JSON path:

```sh
cargo run --release -p capture-probe --bin rgbd-probe -- --config /absolute/private/rgbd.json
```

Configuration schema (device identity and actual profiles belong in private
configuration; these values illustrate structure only):

```json
{
  "version": 1,
  "sdk_library": "/absolute/path/libOrbbecSDK.so",
  "serial": "ACTUAL_SERIAL",
  "color": {"width": 640, "height": 480, "fps": 30, "frame_id": "camera_color_optical_frame"},
  "depth": {"width": 640, "height": 480, "fps": 30, "frame_id": "camera_depth_optical_frame", "unit_policy": "normalize-mm"},
  "pairing": {"clock": "device", "max_delta_us": 20000},
  "seconds": 8,
  "poll_ms": 100,
  "capacity": 4,
  "startup_discovery_seconds": 3,
  "ros": {"bridge": "/absolute/path/libgeminidepthport_ros_bridge.so", "namespace": "/experiment_camera", "stamp_source": "sdk-system-unix"}
}
```

`ros` is optional. `startup_discovery_seconds` is optional and defaults to zero;
all other shown fields are required. No settings are selected from the first
device, and no profile or skew tolerance fallback is used. The 20,000 microsecond
example is an explicit acceptance threshold, not a claim that a device meets it.
`unit_policy` is `require-mm` or `normalize-mm`, with the same pixel semantics as
the single-stream probe. Unknown fields and caller-provided calibration are
rejected.

Exactly one SDK Pipeline enables both exact profiles. Each observed SDK frameset
must supply both frames; there is no matching across framesets and no requirement
that color/depth frame indexes equal each other. The selected clock must advance
for each stream and the skew must fit the explicit budget. Invalid, missing,
repeated or over-budget pairs never enter the output queue. The queue evicts a
whole RGB-D pair, keeping each stream's bytes and metadata together. The current
probe drains every accepted pair immediately, so configured capacity is an upper
bound rather than a claim of asynchronous throughput.

With ROS enabled, calibration comes from the first complete actual frameset's SDK
stream profiles, checked against the requested profiles. No caller intrinsics or
identity extrinsics substitute for missing SDK calibration. Two publishers emit
color/depth `Image`, `CameraInfo`, and frame metadata. The auxiliary publisher
holds actual SDK depth-to-color static TF (parent color optical, child depth
optical) and publishes `NAMESPACE/rgbd/pair_metadata` only after both stream
publish calls succeed. Pair metadata includes both independent sequences, raw
selected-clock times, skew, pairing clock, pair sequence, and the corresponding
ROS stamps for consumer association. SDK translation is converted from mm to m.
The static TF does not include robot hand-eye calibration.

All ROS image stamps still use explicit SDK **system Unix** time; selecting
`pairing.clock=device` only changes the pair-skew comparison. Both mapped image
stamps are validated before publishing either stream. DDS topics cannot be
published atomically: an output failure can leave a partial set of messages, so
consumer acceptance must require the pair-metadata association and both images.
The report counts successful individual bridge calls and completed pair calls,
not delivered ROS messages.

Startup is a bounded polling/discovery interval, not an unreported sleep. Startup
framesets, missing frames, skew failures and other rejections are reported under
`startup_discarded` separately and are never published. With a zero startup
budget, all such observations belong to the strict measurement window. Pair
progression is guarded across the startup-to-measurement boundary. Active
complete-frameset skew min/max/mean and over-budget counts include frames later
rejected by format/depth validation. Any active contract/pair rejection or no
accepted active pairs makes the run fail. SDK aggregation/default operation does
not establish hardware synchronization, equal exposure times, or synchronized
RGB-D optics; these remain explicitly unverified.

## Resident capture service

For deployment, use the [managed Rust backend](../../docs/guides/rust-backend.md).
Standalone service configuration and supervision are documented in the
[resident service reference](../../docs/development/capture-service.md).
