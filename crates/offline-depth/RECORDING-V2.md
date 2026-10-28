# Recording directory v2

The existing bound-pair-directory-v1 source/API is unchanged. Use
`offline-depth --input ABS_DIR --recording-v2 --validate-only` or provide the
existing `--output ABS_DIR --job NAME` export options. `--preserve-counts` exports
exact native u16 counts with explicit scale (also available for v1),
`--normalize-mm` explicitly rounds to millimetres, and `--d2c` warps with the
snapshot calibration. These operations are mutually exclusive. Counts cannot
be recovered from an observed ROS millimetre image.

The version 2 manifest schema is `recording::RecordingManifest`: immutable
snapshot/binding, JSONL record path/hash and positive record count. Records are
streamed with a 64 KiB line cap, and index hash/count are checked at EOF before
commit. Each `RecordingRecord` references exact evidence and both original
pixel buffers by SHA256, with original strides/endian/representation. Optional
ROS topic/header/record timestamps are preserved, not used to invent SDK IDs.
Record ordering is strict in each stream's selected source timestamp and SDK
sequence where known, within a fixed session/snapshot. Color pixels are checked
even though only depth PNGs are exported.

`TaggedEvidence` is `sdk-bound-pair` with a real `BoundPairEvidence`, or
`observed-association` with a real Rust `ObservedAssociation` and explicit
header clock mapper. An optional SDK `source_json` reference preserves a recorded
publisher's exact original JSON bytes and is independently compared to the
parsed pair. Observed associations retain null SDK sequence/raw times when
unknown and cannot claim SDK frameset/hardware sync. Observed CameraInfo and
publisher metadata files are independently hash/geometry/identity/header bound.
This initial directory boundary requires exact CameraInfo header stamps and
identity rectification. Unsupported representations fail explicitly.

For optional native SDK capture, use
`RawRecordingWriter::begin_with_limit(parent,name,snapshot,max_bytes)`,
`append(&native_pair,color_bytes,depth_bytes,endian)` and `finish_guarded(acquire)`.
The callback runs after full on-disk verification and returns an application RAII
guard held through the short publication/COMPLETE/fsync phase. It may reject an
aborted/terminal recording; a shared application mutex can linearize capture
failure against commit without holding the mutex during full validation.
`finish()` is the unguarded offline convenience API.
Call optional `attach_ros_snapshot(&original_ros_snapshot)` before the first append
to retain `ros-snapshot.json` and `snapshot-association.json`. Both envelopes are
independently verified; their content must differ only in depth representation
(native SdkCounts versus ROS Millimetres), with identical snapshot bindings.
These files count toward max_bytes, failures poison the writer, and the reader
revalidates the link. Exports copy the link and original ROS snapshot to
content-addressed blobs with a recording-context sidecar, so status-file
replacement in a later session cannot erase this provenance.

The caller supplies a bounded queue of owned whole pairs; no SDK handle enters
the writer. The pair must describe **actual pre-normalization bytes** with
`normalized=false`, original native stride, source/output scale equal and real
SDK sequence/clock evidence. A normalized ROS pair cannot be reused as native
counts evidence. If snapshot output depth is mm, its immutable configuration
must explicitly bind `source_depth_scale_mm` to the native scale. The writer
retains full original padding, validates the full directory on finish, and
publishes `NAME.partial` with a platform atomic no-replace rename only after successful drain/verification. Files and directories are synchronized; a racing final directory is never overwritten. Snapshot, pixel buffers, evidence, JSONL index, manifest and completion marker bytes all reserve the explicit byte budget before IO. Budget exhaustion is terminal; the recording remains visibly incomplete. The legacy `begin` API uses an unlimited byte budget and should not be used for production live sinks. Errors/drop
retain `INTERRUPTED.json` and never finalize an episode or decide validity.

Exports preserve tagged evidence, record descriptors and original referenced
bytes under `source-blobs/SHA256`. A descriptor's original path is provenance;
its digest resolves the copied bytes even after the source directory is removed.
Existing millimetre manifests retain their schema; counts output explicitly
uses unit `sdk-counts` and `original_sdk_counts {scale_mm}`. A PNG remains a
lossless integer container, not proof of physical depth accuracy. Partial
folders are never successful artifacts, even if a prepared marker exists.
