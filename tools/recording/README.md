# Recording adapters

These adapters consume public capture contracts. They contain no SDK open calls,
robot/episode validity rules, or project-specific bag conventions. Original
pixels, device IDs, topics, calibration and raw CDR are private artifacts; CLI
summaries contain counts and stable error codes only.

## Historical ROSbag2 and MCAP

Build the pure Rust kernels:

```sh
cargo build --release -p capture-observer -p offline-depth
```

Inside an environment with `rosbag2_py`, `rclpy` and `rosidl_runtime_py`:

```sh
python3 tools/recording/bag_import.py \
  --input PRIVATE_BAG --storage-id sqlite3 \
  --config PRIVATE_IMPORT_CONFIG.json --output PRIVATE_NEW_DIRECTORY \
  --observer-engine /absolute/path/capture-observer \
  --offline-engine /absolute/path/offline-depth
```

Use `--storage-id mcap` only where the ROSbag2 MCAP storage plugin is installed.
There is no MCAP or ROS dependency in the Rust conversion kernel. Unsupported
storage/plugins, types, bindings, representations and malformed/truncated input
fail nonzero; no fallback interpretation or fake completion is attempted.

The private configuration is an object with:

- `backend`: `official` or `rust`.
- `snapshot`: path to the actual immutable `SnapshotEnvelope`.
- `storage_representation`: the contract DepthRepresentation, for example
  `{"representation":"millimetres","quantization":"nearest-half-up-preserve-zero-clamp-positive-reject-overflow"}`.
- `bindings`: `color` and `depth` objects, each containing an absolute `image`
  and `camera_info` topic and optional `metadata` String topic. Topics are unique
  and their exact advertised ROS types must match.
- `official` additionally requires `observer_config`, the version-1 Rust
  ObserverConfig with fixed snapshot identity, actual camera geometry, explicit
  header mapper, tolerances, watermark and bounded quality/association policies.
  This initial importer requires zero CameraInfo stamp tolerance and a common
  explicit mapper for the two streams. It never infers host wall mapping.
- `rust` additionally requires `sdk_evidence_topic`, a distinct String topic
  containing recorded complete `BoundPairEvidence` JSON. A summary pair_metadata
  message lacking full bound identity/layout is insufficient. Images are
  resolved by the pair's exact recorded SDK-system timestamp references; more
  than one candidate is an error. Python does not independently pair streams.
  SDK sequences need not equal each other. Unknown raw clocks are never made up.

Official Image/Info/metadata events are decoded and sent to the persistent Rust
observer. Rust returns the **specific input component tokens** it selected;
Python resolves those tokens in a disk index. It does not reimplement tolerance,
nearest-neighbour, watermark or ambiguity rules. At known offline EOF an explicit
terminal watermark seals remaining candidates; it is not recorded as a camera
receipt/exposure time. Any Rust association rejection leaves the import partial.

Every selected original message is saved as exact CDR bytes in `wire-*.cdr`,
with original topic/type and bag record timestamp in `wire.jsonl`. Image payloads
retain original bytes including padding/endian. Generated decoded Info files
and metadata raw UTF-8 JSON are retained and hashed. The v2 Rust directory reader
independently verifies all referenced hashes, two-stream layouts, snapshot
identity, calibration, source times and provenance before the adapter finalizes.
The SDK publisher's original JSON is saved without reserialization and bound
against its parsed tagged evidence. Native source counts and explicitly supplied
scale can be retained; normalized ROS mm cannot reconstruct native counts.

The adapter processes one bag event synchronously. Python holds at most the
current CDR/message/pixels and bounded Rust update; component references are
indexed on disk with a 2 MiB SQLite cache. There is no unbounded in-memory frame
queue. Disk use grows with original recording size; callers must provision a
private output volume and handle IO exhaustion. Existing outputs and partial
folders are never reused. Only successful drain plus independent Rust validation
renames the stage to the final directory. Failure retains `*.partial/FAILED.json`;
partial folders are never complete recordings. The source bag remains intact.

## Public episode adapter example

```sh
python3 tools/recording/episode_adapter.py \
  --context PRIVATE_RECORDING_CONTEXT.json \
  --events-jsonl PRIVATE_QUALITY_QUERY_STREAM.jsonl \
  --output PRIVATE_NEW_EPISODE_EVIDENCE --require-strong
```

This consumes the management `recording-context` v1 response and exact
version-1 quality query/event envelopes. Snapshot, session, generation and
instance bindings are checked. The full original context bytes and quality
stream are saved, including EVENT_GAP, session transitions, disconnects and
finalization facts. `--require-strong` refuses a context that lacks native
restart-gate/session-bound strong protection. Omitting it records the weaker
capabilities as facts; it does not upgrade an official backend to SDK protection.

At EOF this tool commits a *complete evidence copy*, with
`business_validity:null` and `physical_accuracy_verified:false`. It does not
certify that an episode remained valid or that the quality stream was complete.
The application decides that policy from the retained facts. Truncation,
oversized lines, malformed JSON, IO failure or interruption leaves a partial
copy with explicit interrupted state. Event lines are capped at 64 KiB and
streamed without an in-memory episode history. No protection tokens or private
project access are needed; the management context must not contain credentials.

## Repeatable synthetic checks

```sh
cargo build -p capture-observer -p offline-depth
python3 -m unittest discover -s tools/recording -p 'test_*.py'
```

Tests exercise strict JSON, exact CDR byte preservation at the optional ROS
boundary, both actual persistent Rust association/offline CLI paths, SDK raw
sequence preservation, publisher JSON byte identity, protection capability
refusal and interrupted episode copies. The ordinary tests use synthetic
messages and a mocked ROS deserialize boundary; they do **not** prove ROS CDR
interoperability.

For actual ROSbag2 serialization/storage/deserialize tests, in a ROS environment:

```sh
python3 tools/recording/rosbag_smoke.py --storage-id sqlite3 \
  --observer-engine /absolute/path/capture-observer \
  --offline-engine /absolute/path/offline-depth
```

Repeat with `mcap` if that plugin is installed. This creates a tiny synthetic
Image/Info bag with real ROS serialization, invokes the actual adapter CLI,
Rust matching and offline verification, and compares each recorded CDR byte
sequence. It opens no cameras. Local execution without ROS is not claimed as a
passing real-bag test. None of these tools certify physical accuracy, business
consumer compatibility, supported deployment scale or performance.
