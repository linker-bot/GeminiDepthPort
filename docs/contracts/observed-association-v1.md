# Read-only observed timestamp association, version 1

`capture-observer` is a pure Rust library and persistent JSONL executable. It consumes decoded ROS Image, CameraInfo and optional String metadata. It never opens an SDK device or subscribes/publishes by itself. `tools/observation/official_ros_observer.py` supplies the actual read-only rclpy subscription boundary. Python only extracts fields; numerical calibration validation, layout checks, source time mapping, quality state and association are Rust decisions. No image bytes enter the executable or reports.

This is **observed timestamp** association, not an SDK frameset. Outputs explicitly carry `provenance: observed_timestamp`, `same_sdk_frameset: null`, `hardware_sync_verified: false`. Observer session/generation/configuration/calibration/snapshot IDs must be fixed by the caller. Callback order supplies only an observation counter. SDK sequence and raw device/system timestamps remain null unless the supported metadata publisher actually reports them; even then they are labelled publisher-reported, not independently verified. Device time zero is preserved as a reported value, never substituted for an unknown value.

## Private configuration and execution

Build with `cargo build --release -p capture-observer`. Run:

```sh
python3 tools/observation/official_ros_observer.py \
  --config PRIVATE_CONFIG.json --engine target/release/capture-observer \
  --duration-seconds 60 --tick-seconds 0.05
```

The adapter config is an object with `engine_config`, `bindings` and `qos`. `engine_config` serializes `ObserverConfig`; its public Rust structs are the versioned schema. `bindings` has `color` and `depth` objects, each with distinct absolute `image` and `camera_info` topics and an optional `metadata` topic. `qos` requires `reliability` (`reliable` or `best-effort`) and bounded integer `depth` (1–4096). ROS environment/domain/security setup remains explicit deployment configuration. The adapter does not discover devices, guess topic namespaces, change a publisher, or reset on a publisher sequence rollback.

Each expected stream fixes its full CameraModel, rectification R, projection P, encoding, numerical tolerance and metadata requirement. Image endian, strides and checked payload spans are validated. Supported encodings are yuyv, rgb8, bgr8 and 16UC1. CameraInfo binning/ROI must satisfy the configured full-image contract. Supported metadata is version 1 JSON with frame_id, stamp_sec and stamp_nanosec plus optional sequence, device_timestamp_us, system_timestamp_us and session_id. Other formats fail explicitly; arbitrary vendor metadata is not interpreted as SDK evidence.

The source header mapper is explicitly `unknown`, `declared-observed-unix-header`, or `measured-observed-header-offset` with a signed nanosecond offset. Declaring a mapping is a caller assertion; this observer cannot prove camera clock synchronization. Source age is mapped ROS header age, not exposure latency. Monotonic receipt time uses a separate observer-session origin. Wall clock regressions do not silently restart the observer or erase stale/quality episodes.

## Deterministic closure and bounded state

All association tolerances, watermark lateness, capacities, source age/future limits, stale thresholds, event retention, and recovery hysteresis are explicit. Image/Info/metadata candidates form connected components under configured timestamp tolerances. A component closes only after its greatest possible candidate time is behind the watermark. It requires exactly one image and one Info, plus exactly one metadata message when required. Shared candidates or multiple candidates reject the whole component; there is no first/nearest greedy choice.

Completed color/depth frames form a second bipartite component graph under pair tolerance. Pair closure additionally waits for the upstream Image/Info/metadata closure horizon. Exactly one candidate of each role yields a pair. Ambiguous groups and missing components have stable rejection reasons. Tick advances a Unix watermark from receipt time minus explicit lateness; callers can also supply an explicit watermark. Data at/before a committed watermark is late and rejected. Finalization marks unresolved boundary components `observation-window-closed`, without inventing future time or pretending boundary rejection proves transport loss.

Per-component queues, rolling windows, episode events and summaries are bounded. Overflow is counted and reported. Decisions do not depend on hash iteration order or arrival order when the same candidates precede the watermark. No pixels or raw topic/frame bindings appear in Rust responses. IDs are caller-supplied public opaque tokens; private serials must not be used as those tokens.

## JSONL and unified queries

The CLI accepts one `{version:1,command:{kind:...}}` per line. Commands are `image`, `camera-info`, `metadata`, `tick`, `watermark`, `query` and `finalize`; the exact decoded DTOs are in `crates/capture-observer/src/lib.rs` and command envelope in `src/main.rs`. Every mutating command includes a session-scoped `at` except finalize. Image includes optional receipt Unix time. Inputs/config are capped at 64 KiB, duplicate keys are rejected, errors expose stable codes only, and oversized lines are drained before accepting the next request.

Query wraps the shared quality API:

```json
{"version":1,"command":{"kind":"query","request":{"version":1,"query":{"kind":"snapshot"}}}}
```

Replace query with `{"kind":"events","cursor":null,"limit":16}` for the first event page. Subsequent requests use the returned cursor. A retention gap is explicit `EVENT_GAP`, never an empty page implying complete history. Results add bounded association counters/watermark to the shared quality response. Sustained faults update active counts; only episode opening and recovery emit transition events. An observed association is read-only evidence: downstream policy must inspect `continue_allowed`; emitting an association does not authorize a fail-closed consumer to use it. Pending image observations enter the rolling quality window with Unknown contract evidence, never borrowed from the previous CameraInfo. Successful component closure advances contract recovery but does not retroactively rewrite historical window samples. Consequently this initial observer is deliberately conservative under FailClosed: pending historical coverage can keep continue_allowed false until it expires, and continuous delayed closure can keep it false. ReportOnly is the usable monitoring mode; association results remain independently strict. A downstream business authorization policy is outside this observer version.

The ROS adapter prints only the final Rust snapshot. Live consumers needing pair/event streams can use the same persistent CLI protocol. Responses are drained synchronously with a timeout and bounded response memory. Temporary engine configuration is mode 0600 and removed after startup. Callback failure exits with a redacted stable error.

## Validation boundary

Synthetic tests exercise actual CLI persistence, malformed/oversized input recovery, exact/ambiguous matching, arrival permutations, delayed Info, explicit offset mapping, unknown SDK fields, metadata provenance, bounded overflow, watermark regressions, Tick-driven stale and decoder non-access to pixel data. These tests require neither ROS nor an SDK. The rclpy boundary is implemented but has not been hardware-qualified in this PR; no business consumer compatibility, physical calibration accuracy or transport delivery guarantee is claimed.
