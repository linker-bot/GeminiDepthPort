# Minimal C++ SDK/ROS reference

`sdk_ros_reference.cpp` is a deliberately small reference using the fixed public
Orbbec SDK 2.10.6 C++ wrapper and the **same** dynamically loaded ROS C ABI bridge
as the Rust worker. It is not OrbbecSDK_ROS2 and must never be labeled an official
ROS-driver result. SDK headers and libraries are referenced, not copied here.

Build inside the existing official ROS capture image (C++17, nlohmann-json headers,
SDK headers and library available); building/self-testing does not open devices:

```sh
SDK_ROOT=/absolute/fixed/sdk bash tools/validation/build_sdk_ros_reference.sh /private/output/sdk-ros-reference
```

The build emits a sidecar containing the SDK, executable and source SHA-256 hashes.
Record the actual ROS bridge hash and image digest as well. `SDK_ROOT/lib` is the
linker RUNPATH; verify the actual loaded SDK artifact matches the Rust image
hash before comparing results. The runtime additionally requires version 2.10.6.

Run **only after** the normal worker has released the physical device:

```sh
ROS_LOCALHOST_ONLY=0 FASTRTPS_DEFAULT_PROFILES_FILE=/absolute/loopback-udp.xml \
  /private/output/sdk-ros-reference --config /absolute/private/capture.json --seconds 30
```

The existing private `capture-service` JSON can be used. This tool reads explicit
serial, color/depth profiles and frame IDs, normalization policy, pairing budget,
ROS bridge/namespace/system-Unix mapping, and startup discovery allowance. Service
supervision fields are not applied. The reference intentionally requires both
profiles to be 640×480@30, color YUYV, raw Depth16, and a predeclared device-clock
pair budget of 20,000 µs. It never selects a first device or another profile.
No real config, serial, intrinsics, distortion or extrinsic values are printed.
Exceptions are converted to stable redacted error categories and SDK logging is
disabled before Context/device creation.

The same single SDK Pipeline supplies both frames. Actual first-frame SDK profiles
provide CameraInfo and depth-to-color TF. Distortion mappings and R/P construction
match the Rust adapter and reject unsupported models. Both image/frame-metadata
triplets are followed by associated pair metadata; all three bridge handles bind
one random 128-bit session before publishing. Image stamps use SDK system Unix
microseconds. Selected device timestamps and independent per-stream indexes must
advance, and pairs outside 20 ms are rejected. There is no cross-frameset matching
and no exposure synchronization claim.

Strict depth requires scale exactly 1 mm. Explicit normalization uses the actual
SDK scale, f64 multiplication from f32 scale, nonnegative half-up rounding, invalid
zero retention, nonzero-to-1 mm clamping, and whole-frame overflow rejection. It
allocates an owned packed little-endian u16 buffer even for input scale 1 mm.
Raw color/depth SDK frames stay owned through synchronous publish; the bridge
copies ROS messages before returning. No SDK frame pointer escapes its lifetime.

The SDK constructor attempts frame synchronization by default in SDK 2.10.6.
Both this reference and the current Rust adapter retain that default and default
ANY_SITUATION aggregation. Neither explicitly sets alignment, exposure, white
balance, device clock sync, recommended software filters or hardware denoising.
Compare this with the official driver's property writes and explicit sync setting
separately. Do not force a single profile hash across these unequal behaviors.

Use the same external consumer, QoS, transport, image hash/scale/CameraInfo/TF
acceptance, warmup, CPU topology and resource monitor for both implementations.
Counter and publish-call success do not prove subscriber delivery. The reference
lacks the Rust service's supervisor, status fsync, recovery and readiness work;
report those structural differences rather than attributing total CPU difference
to language alone. Startup rejects are counted separately; any active rejection
or no published pair makes the reference fail.

SIGINT/SIGTERM end the finite polling loop and stop the Pipeline. Native SDK stop
or publish calls can still hang; run under the same process/container deadline
supervisor used for the experiment. A signal is not a recording-completion
certificate. `--self-test` validates quantization/zero/clamp/overflow behavior
without constructing an SDK Context or opening hardware.

The current reference additionally queries each frame's SDK stream profile to
check its width/height/FPS/format, whereas the Rust worker validates per-frame
metadata and reads full profiles for calibration initialization. This extra
reference-side profile validation is an explicit cost difference. Intrinsics,
distortion and extrinsics are read only once from the first complete frameset,
not per frame. Treat results as implementation comparisons rather than isolating
language overhead.

## Gated official-driver comparison

`benchmark_services.py` retains its default `cpp/rust/rust/cpp` reference mode.
For a separate official-driver experiment, supply an explicit private parameter
file instead of a C++ executable:

```sh
python3 tools/validation/benchmark_services.py \
  --image local/gdp-rust:service \
  --official-params /private/official/params.parity.json \
  --capture-config /private/capture.json --observer-spec /private/observer.json \
  --output-dir /private/new-official-benchmark --device /dev/bus/usb/001/002 \
  --domain 49 --seconds 40
```

This command is **plan only**: no Docker calls, devices, output directory or file
copies. Execution requires both `--execute --devices-released` and root; it refuses
an existing output path or an already mapped device/test workload and never stops
other workloads to obtain a device. Use only after management has released both
cameras. The same immutable image must contain the Rust runtime and the official
`orbbec_camera` installation under `/ws/install`.

Official mode alternates `official/rust/rust/official`. It preserves the existing
8-second producer stabilization, common 8-second consumer discovery, shared
ready/go gate and exact resource sampling window. Both implementations use the
same standard-wire observer code/spec hash, platform, SDK artifact hash, loopback
UDP transport and private IPC. The observer checks actual Image/CameraInfo and
static TF projection, including legal composed static TF trees, rather than
requiring the official driver to emit the Rust metadata extension.

The official parameter file must explicitly agree with capture serial, shared
optical frame prefix/camera name, exact profiles, native YUYV/Y16 formats, DEFAULT
reliable QoS, system timestamp source, enabled frame sync and ANY aggregation.
Software filters, depth alignment, extra streams and point clouds must be
explicitly disabled. Extra configuration-file references and active image
transforms are refused. If namespace/frame overrides are supplied, they must
match capture bindings. Caller parameters are checked and copied byte-for-byte;
the runner never silently changes them. A generated public launch script reads
only the staged explicit capture/parameter files and creates the composable node
inside the capture namespace. No parent-directory or unrelated configuration
copying is used, and no C++ binary is read or staged in official mode.

After workspace setup, the runner hashes the SDK library actually mapped by the
official component process; a different library path containing different bytes
fails before measurement. Each trial still verifies the configured SDK/bridge
artifact hashes in the same image. The official driver does not use the Rust ROS
bridge: bridge equality controls the common image artifact, not its active path.

A matching `profile_sha256` in this mode describes **only** common standard-wire
geometry, encoding, millimeter units, raw alignment, system time and bindings. It
does not assert identical internal processing. The report explicitly lists
possible official initialization writes to exposure/AWB/laser/hardware-filter
and device-clock state; Rust caller code does not make those writes. Official
queues, SDK aggregation, depth quantization, metadata-extension work and lack of the Rust caller's 20 ms
pair guard are separate implementation differences. Thus this is not a causal
language-speed comparison. Actual SDK calibration, TF, scale or wire acceptance
failure remains a failed trial; no tolerance, spec or source fields are relaxed
to obtain a performance number. Plan reports select the official mode but do not
claim the official driver has been compared until all measured trials pass.
