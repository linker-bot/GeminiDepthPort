# Public offline depth entry point

This binary consumes a directory of public capture-contract snapshots, bound-pair
evidence and raw16 depth files. It has no SDK, ROS, Docker, MCAP or live recording
gate dependency. Inputs are historical evidence; successful conversion does not
certify live provenance, physical calibration accuracy, hardware synchronization,
or source freshness relative to the current clock.

```
cargo run -p offline-depth --offline -- --input /absolute/source --validate-only
cargo run -p offline-depth --offline -- --input /absolute/source \
  --output /absolute/export-parent --job original --mode both --capacity 2
cargo run -p offline-depth --offline -- --input /absolute/source \
  --output /absolute/export-parent --job registered --d2c --pair 17 --mode png
```

`--mode png|tar|both` defaults to PNG directory. `--capacity` is a bounded blocking
queue of 1–16 complete encoded-frame/evidence envelopes, default 2. It never
implements a drop policy. `--pair` selects exactly one already-associated pair;
it does not search, interpolate or associate independent frames. Without it,
pairs are processed in the explicitly listed order. Pair sequence and both
source sequences/timestamps must advance, with a fixed clock source; receive
monotonic offsets must not regress. Selected-pair validation deliberately does
not read unselected payloads.

`--validate-only` reads and validates all selected payloads and performs the
requested numerical conversion/PNG encoding without creating output. It rejects
output/job arguments. Duplicate/unknown arguments and ambiguous operations fail.
There are no devices, profile fallbacks, network lookups or timestamps invented
by the CLI.

## Directory FrameSource version 1

The directory must contain `manifest.json` using this exact typed structure:

```json
{
  "version": 1,
  "source_kind": "bound-pair-directory-v1",
  "snapshot": "snapshot.json",
  "binding": {
    "instance_id": "64 lowercase hex from the snapshot",
    "session_id": "32 lowercase hex from the snapshot",
    "generation_id": "32 lowercase hex from the snapshot",
    "configuration_id": "64 lowercase hex from the snapshot",
    "calibration_id": "64 lowercase hex from the snapshot"
  },
  "pairs": [{
    "pair_sequence": 17,
    "evidence": "pair-17.json",
    "evidence_sha256": "64 lowercase hex SHA256 of exact evidence file bytes",
    "depth": "depth-17.raw16",
    "depth_sha256": "64 lowercase hex SHA256 of exact raw16 file bytes",
    "endian": "little",
    "storage_representation": {
      "representation": "millimetres",
      "quantization": "nearest-half-up-preserve-zero-clamp-positive-reject-overflow"
    }
  }]
}
```

The strings above explain IDs; a real manifest uses the actual hex IDs. The
snapshot is `capture_contract::snapshot::SnapshotEnvelope`; it is strictly
parsed, hash-verified and checked against the independently declared manifest
binding. Evidence is `frame_core::BoundPairEvidence`, with both contexts bound
to that snapshot and its session/configuration/calibration. It retains SDK
frameset association and source times; no hardware-sync claim is added.
`calibration.models` must contain `color` and `depth`. Actual evidence dimensions
must match those models, and pair delta must satisfy the snapshot quality
policy's explicit pairing bound. Offline conversion does not apply wall-clock
age gates or claim that every live quality-policy condition has been rechecked.

`storage_representation` must exactly equal `snapshot.content.depth`; the
SDK-precision evidence output scale must exactly match that representation when
promoted from f32 to f64. For preserved SDK counts, use:

```json
{"representation":"sdk-counts","scale_mm":0.10000000149011612}
```

That example is the exact f64 promotion of SDK `0.1f32`; adapters must preserve
the actual source precision instead of rounding calibration/unit evidence.
Never relabel SDK count bytes as millimetres. Original count export requires
explicit `--normalize-mm`; it rounds half up, preserves raw zero, clamps
positive submillimetre values to 1 mm and rejects frame overflow. D2C explicitly
consumes the declared source scale and quantizes only final color-Z winners;
`--d2c --normalize-mm` is rejected to prevent an unrequested double stage.
`--normalize-mm` is harmless for input already in millimetres.

Evidence's `depth.stride * depth.height` must exactly equal the raw16 file length,
including every row's padding. Endian may be `little` or `big`; no native-endian
assumption is made. Inputs are bounded to 4 MiB per JSON, 8192 manifest pairs and
64 MiB per raw file; geometry additionally has d2c-core dimension/pixel bounds.
Only relative normal path components are allowed, and canonical paths must stay
inside the source directory. Escaping symlinks, duplicate JSON keys, unknown
schema fields, truncation, bad hashes and mismatched bindings fail.

D2C uses exactly one explicit depth-frame→color-frame transform from the
snapshot, in metres with a proper row-major rotation. It rejects missing,
ambiguous, reversed-only, unsupported equidistant or invalid models. It does not
compose unspecified transforms, infer missing calibration, fill holes or use an
SDK output as a golden image. See [d2c-core](../d2c-core/README.md) for optical-Z,
rounding, signed projection bounds and unquantized z-buffer semantics.

## Output and failure

A successful job is one new directory containing `COMPLETE`,
`manifest.jsonl`, requested PNG/TAR output, `snapshot.json`, and exact evidence
copies named `pair-evidence-{pair_sequence:020}.json`. Each output frame's record
links the original depth context, pair sequence, evidence SHA256, and kind
`original_millimetres` or `d2c_color_z_millimetres`. D2C output dimensions belong
to the color model, while its source context remains the original depth context.
The manifest's output dimensions/kind identify this derivation. The exported
snapshot still describes the **source representation**, not a newly acquired
millimetre frame. Sidecars permit hash resolution after the source is removed.

All output PNGs are grayscale u16 millimetres, with 0 invalid. TAR and directory
modes use identical PNG bytes. Jobs never overwrite previous complete or partial
jobs. Writer/reader/conversion failure returns nonzero, prints a stable redacted
JSON `complete:false`, and retains an unfinished `.partial` directory when one
was already created. Only producer success plus complete consumer drain can
commit the directory; an empty/incomplete job is never marked complete. A source
failure before job creation creates no output. CLI error output omits source
paths, IDs, serials and calibration values. If the final directory rename succeeds
but parent-directory synchronization fails, the distinct
`OUTPUT_COMMIT_UNCERTAIN` error reports a potentially visible final artifact;
it must be reconciled rather than retried under the same job name. Success reports counts, not latency
or performance claims.

The public Rust abstraction is `FrameSource` with `DirectoryFrameSource::open`,
`validate` and `run`. Source frames own pixels and exact evidence JSON together.
The output worker receives each encoded image and its evidence in one bounded
envelope; pair evidence is read and written incrementally. Queue limits bound
pending envelopes, not whole-process RSS; source/conversion/PNG buffers and the
bounded d2c-core ray/output storage also occupy memory.

## Reproducible checks

`cargo test -p offline-depth --offline` launches the real CLI for normalized
original PNG/TAR, SDK-count normalization, nonidentity D2C color-Z, validate-only,
selection, wrong units/dimensions/snapshot/session, bad hashes, malformed JSON,
invalid paths/arguments, cross-directory symlinks, independent per-stream
sequence progression, clock-source changes, big-endian padded rows, and a
later-frame failure after one frame was written.
It also deletes the input before resolving exported snapshot/evidence hashes.
All fixtures are artificial; no SDK comparison or performance test is claimed.
