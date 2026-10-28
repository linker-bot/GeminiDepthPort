# Experimental ROS output bridge

This is a thin ROS Humble `rclcpp` publisher behind the versioned C ABI in
`include/gdp_ros_bridge.h`. Acquisition and SDK frame lifetime remain in Rust.
The bridge makes one explicit copy into an owned `sensor_msgs/Image` per call,
publishes matching `sensor_msgs/CameraInfo`, then emits an allowlisted
`std_msgs/String` JSON message on the explicitly bound metadata topic.

All three publishers use RELIABLE, VOLATILE, KEEP_LAST depth 10. Topics must be
distinct absolute names inside the explicitly supplied namespace. Node/frame
names and every topic are validated on both sides. The bridge uses its own ROS
context, ignores global ROS command-line remapping arguments and performs no
device access. Environment-selected ROS domain/RMW/transport still apply.
Creation queries `get_actual_qos()` for each local publisher and rejects a
different reliability, durability, history or depth. The metadata flag
`publisher_qos_verified` records this constructor invariant. Humble Fast DDS
discovery can report UNKNOWN history and depth 0; those graph values are not
used as evidence for the publisher's actual history/depth configuration.

## Calibration and time contracts

The caller must provide observed private calibration, not a sample fallback:

- Version 1 JSON has exactly `version`, `width`, `height`, `frame_id`,
  `distortion_model`, `k`, `d`, `r`, `p`.
- Intrinsics/projection matrices require finite numbers, positive focal lengths
  and valid homogeneous rows; rectification requires a proper rotation.
- Supported distortion models/counts are `plumb_bob`/5,
  `rational_polynomial`/8 and `equidistant`/4.
- Calibration dimensions and optical frame must match every image. Crop,
  binning and fabricated matrices are not silently inferred.
- YUYV must be packed, even-width `yuyv`; depth must be little-endian `16UC1`
  with output scale exactly 1 mm. Fractional SDK depth requires explicit
  normalization before publication, retaining the source scale in metadata.
- The only supported stamp mapping is the caller-declared SDK **system** Unix
  microseconds multiplied by 1000. No device-clock mapping, exposure timing
  claim, host-now fallback or clock repair is performed. Stamp and sequence
  must each advance within a publisher instance.
  Raw device time may legitimately be zero at its clock origin and is preserved
  as zero, distinctly from an absent timestamp; mapped system/Unix time must
  remain positive.

The metadata message carries version, source sequence, shared ROS stamp,
optical frame, encoding/layout, optional raw device timestamp, raw SDK system
timestamp, declared stamp mapping, source/output depth scale and normalization
flag. It describes the associated published frame; it does not certify physical
exposure time, synchronization, depth accuracy or cross-topic atomic delivery.
The RGB-D path can additionally create one auxiliary context per camera using
the separately versioned `gdp_ros_aux.h` ABI. It publishes a static transform
derived from the **same SDK session's actual depth/color frame profiles**.
The SDK equation is `p_color_m = R_depth_to_color * p_depth_m + t_m`, with its
translation converted from millimetres to metres. TF parent is the color optical
frame and child is the depth optical frame: TF stores this child-to-parent
mapping directly, without inversion. Proper rotation/finite translation checks
run on both sides before matrix-to-unit-quaternion conversion. `/tf_static`
uses RELIABLE/TRANSIENT_LOCAL/KEEP_LAST depth 1, with local actual QoS checked.
No robot mounting, hand-eye or cross-camera transform is fabricated.

The auxiliary context also publishes explicit `pair_metadata` evidence after
both streams' Image/CameraInfo/frame-metadata calls succeed. It carries the pair
sequence, each SDK source sequence, each independent published ROS stamp, the
selected raw pairing clock/timestamps, delta, explicit SDK-frameset provenance
and `hardware_sync_verified:false`. Same SDK frameset plus a configured software
time-delta budget does not certify hardware exposure synchronization. Images keep
their own stamps; equal stamps are never manufactured to imply synchronization.

## Build and synthetic wire verification

In a ROS Humble development environment containing `rclcpp`, `sensor_msgs`,
`std_msgs`, CMake and a C++17 compiler:

```sh
. /opt/ros/humble/setup.sh
cmake -S ros -B /tmp/gdp-ros-build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/tmp/gdp-ros-install
cmake --build /tmp/gdp-ros-build --parallel 2
cmake --install /tmp/gdp-ros-build
python3 ros/tests/smoke_ros.py \
  --library /tmp/gdp-ros-install/lib/libgeminidepthport_ros_bridge.so \
  --namespace /gdp_synthetic_smoke
python3 ros/tests/smoke_aux.py \
  --library /tmp/gdp-ros-install/lib/libgeminidepthport_ros_bridge.so \
  --namespace /gdp_synthetic_aux_smoke
```

The synthetic wire test publishes declared artificial 2x2 fixtures only. It
checks actual ROS delivery, byte equality after caller-buffer mutation,
CameraInfo equality after caller-calibration mutation, matching stamps,
per-frame metadata, local actual QoS verification and graph-observable
reliability/durability. It bypasses Rust for malformed C ABI inputs.
It does not require the SDK or access a camera, and its synthetic matrices must
never be reused as physical calibration.
The auxiliary synthetic test starts its subscriber after the one-time static
publication, checks transient-local delivery, a nonidentity rotation/translation
known point in both directions, and pair metadata with unequal image stamps.
It deliberately uses artificial fixtures and is not physical calibration proof.

Hardware-independent Rust tests load a compiled mock native bridge and verify
ABI mismatch, ownership, failed-create cleanup, timestamp/sequence checks and
failure-state handling. The independent native validator also runs without ROS:

```sh
cargo test --offline -p ros-output
c++ -std=c++17 -Wall -Wextra -Werror -Iros/include \
  ros/tests/validation.cpp -o /tmp/gdp-ros-validation
/tmp/gdp-ros-validation
```

## Native boundary limits

The library path must be absolute and trusted: `dlopen` executes native code.
Version/symbol checking is not binary authentication. C caller spans and strings
must be readable for their declared lifetime; arbitrary dangling pointers cannot
be made safe by validation. The bridge validates lengths, schemas, bounds and
time/layout semantics before reading payloads, catches C++ exceptions at the
boundary and retains no caller pointers. Rust uses `Rc` confinement and does not
assert `Send` or `Sync`.

Each publish is synchronous with respect to the owned copy, not with subscriber
delivery. A ROS error can occur after one topic was published; terminate that
experimental session instead of claiming an atomic rollback. Queues, DDS
backpressure and shutdown can block in native code. No end-to-end zero-copy,
latency target, drop-free recording or hardware acceptance is claimed.
