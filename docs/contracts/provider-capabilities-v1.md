# Generic provider interfaces: implementation and acceptance checklist

These tasks are additive public software capabilities. They contain no deployment
bindings, device identities, private geometry, recorder policy or application
output conventions. This change does not claim hardware or production acceptance.

## Task 1 — capability negotiation and offline build identity

Status: implemented, software acceptance covered by automated tests.

Files:

- `crates/capture-contract/build.rs`, `src/capabilities.rs`, `src/lib.rs` in that crate.
- `src/main.rs`, `src/server.rs`.
- `crates/offline-depth/src/main.rs`, `src/lib.rs`, `src/recording.rs`.
- `crates/depth-output/src/lib.rs`, dependency metadata and lockfile.

Interfaces:

```sh
geminidepthport capabilities
geminidepthport capabilities --require '{"protocols":{"quality_batch":1}}'
geminidepthport build-info
offline-depth --capabilities
offline-depth --capabilities --require '{"protocols":{"offline_input":2,"offline_diagnostics":1}}'
offline-depth --version
```

HTTP GET `/api/capabilities`, `/api/build-info`; optional `require` query parameter
on capabilities is URL-encoded strict JSON. Existing host/origin policy applies.
CLI discovery does not load inventory, contact Docker, create deployment state or
open devices. HTTP discovery requires the normally running management server.

The protocol catalog enumerates supported versions, output modes, distortion
models, numerical algorithm version and queue limits. `--require` selects exact
supported versions; unsupported or malformed requirements fail, never downgrade.
The catalog describes the software distribution, including its separate binaries;
it does not certify installed helpers, current hardware, an active ACK or live
readiness. Query the actual offline executable before relying on its capabilities.

Build info contains component/package version, build-time source revision and
working-tree dirty state, executable SHA256, hash-source method and
`identity_attested:false`. Unknown source metadata is null. Extracted source trees
must not inherit a parent repository's revision. No timestamp or private source
path is embedded. Linux hashes `/proc/self/exe`; other systems report that their
hash came from the executable path, which is not mapped-image attestation. The
hash is cached once per process. A Git revision/dirty flag is self-reported build
metadata, not a reproducibility or supply-chain signature claim.

Offline success reports add `build` and `d2c_diagnostics`; old report versions and
fields retain their existing meanings. Each export also saves `build-info.json`,
including numerical algorithm declarations, independently of the source snapshot.

Acceptance:

- Both CLIs answer without input recordings or hardware; management CLI answers
  even when the specified deployment root does not exist and leaves it absent.
- Supported exact requirements succeed; unknown protocols/versions, duplicates
  and malformed JSON fail. Capability discovery never claims worker readiness.
- Exported build identity equals the executing CLI report; source snapshot and
  evidence identities remain untouched. Old CLI/recording tests continue passing.

## Task 2 — D2C diagnostics

Status: implemented, software acceptance covered by numerical and CLI tests.

Files: `crates/offline-depth/src/lib.rs`, `src/recording.rs`, `src/job_control.rs`,
`crates/depth-output/src/lib.rs` and their integration tests.

Each D2C output ordinal has a private immutable
`d2c-diagnostics-{ordinal:020}.json` sidecar containing version, algorithm version,
source context, pair sequence and counters. These retain `source_pixels`,
`zero_depth`, `invalid_ray`, `nonfinite`, `behind`, `projection_failure`,
`out_of_bounds`, `candidates`, `collisions`, `replaced`, `occluded`, `equal_z_ties`,
`valid_output`, and `clamped_nonzero`. The CLI reports aggregate counters for
successfully converted frames. Aborted-frame counters are not invented.

Failure in a CLI-controlled job attempts an owner-only `FAILED.json` in its
already-created partial directory. It records stable error/detail codes, processed
count and last observed pair sequence. "Last observed" does not claim that the
last successfully decoded pair caused a later source-read error. Public stdout
continues to omit source paths, IDs, serials and calibration values. Private
sidecars are not suitable for public issue attachments without redaction.

Acceptance:

- Known translated geometry produces expected color-Z and exact counters.
- v1 and v2 export paths retain diagnostics and build identity without changing
  PNG/TAR pixels, source scale, calibration or original evidence.
- Overflow remains `WARP_FAILED` / `D2C_OUTPUT_OVERFLOW`, fails the entire frame
  and cannot create a completed job. No filtering, clipping or silent fallback.
- Sidecar write failure poisons the job. Sidecars cannot overwrite existing files
  or use arbitrary names; a racing final destination leaves no prepared COMPLETE
  marker in the failed partial directory and does not alter the destination.

## Task 3 — offline progress and cooperative cancellation

Status: implemented, including actual SIGTERM subprocess acceptance.

Files: `crates/offline-depth/src/job_control.rs`, `src/main.rs`, `src/lib.rs`,
`src/recording.rs`, `crates/offline-depth/Cargo.toml`, `Cargo.lock`.

CLI: optional `--progress /absolute/new-progress.jsonl`; its parent must exist.
The file uses exclusive creation and owner-only permissions. It cannot replace
an input, existing progress file or symlink. Progress is opt-in, including during
validate-only; normal validate-only still creates no output.

Library: single-use `JobControl::new`, `cancel` and `execute`. Cancellation handles
may be cloned across threads; the active producer uses thread-local context, so
independent library jobs do not share cancellation flags. A completed controller
cannot be reused. Only the standalone CLI explicitly installs SIGINT/SIGTERM
handlers. Existing library entry points remain usable without a controller.

Progress JSONL version 1 records `running` initially and every 100 processed
pairs, then `complete`, `failed` or `cancelled`. Counts describe processed input
pairs, not consumer receipts or bytes durably committed. Aggregate statistics use
bounded memory; progress disk size grows with recording duration.

Cancellation is cooperative at frame boundaries and before commit. It does not
promise to interrupt an OS-level blocked read/write, a synchronous PNG operation,
or the final atomic commit already in progress. SIGKILL cannot create a terminal
progress event. Cancellation returns nonzero `JOB_CANCELLED`, drains/releases the
bounded worker and leaves partial output without COMPLETE. A cancellation arriving
after commit does not turn a successful export into a false aborted artifact.
Resume is explicitly unsupported: retry with a fresh job name.

Progress/FAILED writes are best effort on a failed full/unwritable disk; absence
of a terminal progress line never proves success. A terminal progress write error
after an otherwise successful operation is `PROGRESS_FINALIZATION_UNCERTAIN`;
inspect the actual output before retrying. Existing `OUTPUT_COMMIT_UNCERTAIN`
semantics remain in effect for directory durability uncertainty.

Acceptance:

- A real CLI child receiving SIGTERM reports cancellation and has no completed
  output; direct library cancellation is also covered.
- Progress is strict JSONL with initial/terminal states and accurate counts.
- Existing paths/relative progress paths fail without overwriting source files.
- Existing queue, byte-preservation, no-replace and failure tests still pass.

## Task 4 — bounded batch quality reads and cursor windows

Status: implemented; query semantics and actual HTTP transport are tested.

Files: `src/quality_batch.rs`, `src/lib.rs`, `src/main.rs`, `src/server.rs`,
`tests/provider_capabilities.rs`. Existing `managed_quality::query` remains the
single authority for producer validation, privacy and event-page continuity.

CLI:

```sh
geminidepthport --root "${DEPLOYMENT_ROOT:?}" quality-batch --request "${PRIVATE_REQUEST:?}"
geminidepthport --root "${DEPLOYMENT_ROOT:?}" quality-window --request "${PRIVATE_REQUEST:?}"
```

HTTP GET `/api/quality/batch?request=...` and `/api/quality/window?request=...`,
with URL-encoded strict JSON. Requests are limited to 64 KiB and reject unknown
fields. They do not mutate inventory, gates or capture sessions.

Batch request version 1:

- `cameras`: one to three unique entries.
- Entry: `camera`, required `expected`, optional `cursor` and `limit` (1–1024).
- Expected identity: actual `generation_id`, `instance_id`, `session_id`,
  `snapshot_id`; all must match. Explicit null snapshot permits terminal unbound
  facts but never authorizes recording or fabricates calibration.

Each entry reads current/events/current and checks the expected binding on all
three. Responses preserve full original facts and unknown scopes, report elapsed
read time, `atomic_group_snapshot:false` and `business_validity:null`. Queries
still run per camera; the current implementation reduces caller process launches,
not native file-read or Docker witness work. It does not claim a simultaneous
cross-camera snapshot or independent proof of protected recording.

Window request version 1: `camera`, `expected`, `from_cursor`, `to_sequence`,
optional `limit`. The interval is `(from_cursor.after_sequence, to_sequence]` in
one session. Upper bounds not yet observed fail. Responses preserve original
source metadata and gap facts; `window_complete` is true only when the upper
bound is reached with no gap. A bounded incomplete page returns its next cursor
and `has_more`; clients repeat with the same upper bound only while `has_more`
is true. When the entire requested window has been evicted, the response preserves
`EVENT_GAP` and the actual unchanged cursor, returns `has_more:false`,
`window_complete:false` and `terminal_reason:"WINDOW_EVICTED"`. This is a terminal
incomplete window, not a successful read or an invented acknowledgement.
Session changes and
wrong cursor origins fail, never stitch sessions or hide unknown scopes.

This initial interface operates on current retained feeds. Existing explicit
historical session-summary queries remain available; full historical event
window reconstruction is not promised. Retention is bounded by producer history,
and EVENT_GAP remains an explicit incomplete result.

Acceptance:

- Group limit, duplicates, cursor origins and per-page bounds are enforced.
- Every before/events/after identity must match; any mismatch rejects the batch.
- Unknown future/recorder scopes survive; terminal null snapshots stay unbound.
- Pagination cannot claim completion early; gaps stay visible and future upper
  boundaries fail. No business-validity or hardware-sync decision is emitted.
- Actual loopback HTTP discovery/negotiation and invalid window requests pass
  under the existing origin policy. Device-backed delivery remains a separate
  environment acceptance, not established by these synthetic tests.

## Re-run acceptance

```sh
cargo fmt -p capture-contract -p depth-output -p offline-depth -p geminidepthport --check
cargo clippy --locked -p capture-contract -p depth-output -p offline-depth -p geminidepthport --all-targets -- -D warnings
cargo test --locked -p capture-contract -p depth-output -p offline-depth -p geminidepthport
```

Tests use artificial geometry, temporary private stores, fake management queries
and actual local CLI/HTTP subprocesses. They open no cameras and make no physical
accuracy, supported-profile, production-scale or final-release acceptance claim.
