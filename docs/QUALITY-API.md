# Bound quality facts and recording context

The machine interface is token-free and reads actual producer evidence, not
configured intentions. It verifies the current Docker ownership/generation,
instance, session, snapshot reference and immutable configuration bytes before
and after reading owner-private regular files. The source remains explicit:
Rust is `sdk-worker`, the official ROS path is `observer`. An observer session
never becomes an SDK session; unknown SDK counters/timestamps remain null.

```
geminidepthport --root /absolute/deployment quality-current camera_a
geminidepthport --root /absolute/deployment quality-events camera_a --limit 50
geminidepthport --root /absolute/deployment quality-events camera_a --cursor '{"origin_session_id":"...32hex...","after_sequence":123}' --limit 50
geminidepthport --root /absolute/deployment quality-session-summary camera_a
geminidepthport --root /absolute/deployment recording-context
geminidepthport --root /absolute/deployment recording-context --require-strong
```

Equivalent local Web endpoints are `/api/quality/current`,
`/api/quality/events`, `/api/quality/session-summary` with `camera`, optional
`cursor` JSON and `limit` query parameters. `/api/recording-context` accepts
`require_strong=true`. They inherit the console's loopback/origin policy. Quality
errors contain stable codes, never raw private diagnostics. The Web summary is
an explicit refresh of producer state and event sequence, not a business-valid
or invalid decision.

Event pages preserve complete event facts, including Recorder and future scope
objects. They never whitelist away an unfamiliar scope or violation. Cursor
origins are checked; evicted events produce `EVENT_GAP`, and limits are bounded
to 1..1024. A different session feed requires a new cursor. The quality query
may expose terminal/degraded facts while the container supervisor is running;
`worker_ready` and `status_age_ms` distinguish them from live capture. A live
session without a finalized summary reports `available=false`; it never
manufactures a successful finalization. Stopped containers cannot be treated as
live evidence, and callers should export finalized facts before removal.

`recording-context` contains full immutable snapshot envelopes and identity
bindings plus controlled quality endpoints. Treat it as a private recording
artifact: configuration can include device identity. Protection capabilities
are explicit. Rust strong capability additionally requires the actual persistent
control mount and configured native gate. `session_bound` requires an active v2
guard matching every current instance/session/snapshot. The official v1 guard
cannot prevent internal SDK reopen, so `native_restart_gate`, `session_bound`
and `strong_guarantee_available` are false; `--require-strong` rejects it.
Physical accuracy remains unverified. Consumers record these facts without
replacing their own task/episode validity policy.

Quality queries also work for a terminal worker that failed before observing a
calibration snapshot. In that case `snapshot_id` remains null; the query is still
bound to the actual Docker container, supervisor instance, worker session/PID,
generation and immutable runtime-config hash. This does not authorize native
recording or invent a snapshot. Official status must explicitly retain observer
identity and `native_session_verified:false`, including degraded/terminal states.
`worker_reported_ready` preserves the producer fact; `worker_ready` additionally
requires a live process witness, fresh status, and (for SDK workers) a recent
reported pair. Old ready files are not presented as current live readiness.

Current/event revisions must agree. The bounded producer event page must be
contiguous through its declared latest sequence; internal/tail omissions are
rejected rather than converted into a false complete page. Unknown scope facts,
including terminal `recorder` episodes, are preserved. Concurrent file revisions
return `QUALITY_EVIDENCE_CHANGED` so callers can retry. A session summary is
available only when it matches the finalized session in the producer snapshot.

Configured native gate capability is separate from active protection.
`native_gate_ack_verified`/`session_bound` additionally require the complete
current binding, runtime hash, active manager request, identical private native
control file and matching ACK digest/PID/session. A missing/stale/corrupt gate or
ACK cannot be inferred healthy merely because the saved manager guard says
active. Context queries recheck capture identity after reading protection facts.
`strong_guarantee_available` describes configured capability, not an already
active protection lease; consumers must check `session_bound` for that evidence.

## Persistent session history

New schema-2 generations bind private host evidence at
`state/generations/<generation>/evidence/<camera>` into `/tmp/capture` (Rust) or
`/tmp/capture-observer` (official observer). Directories are owner-only mode 0700;
JSON evidence is mode 0600. The generation manifest declares
`quality_evidence_version: 1`. Mutable evidence is intentionally outside the
immutable configuration file hashes. Sealed SDK/ROS snapshot files retain their
content IDs. Re-render old bundles to obtain these mounts; earlier tmpfs evidence
cannot be recovered retrospectively. Removing a container or normal `down` does
not remove this host history. Protect and back up it as private calibration data;
there is no automatic retention/deletion policy in this change.

Historical access is explicit and does not require a running container or a
current inventory/generation pointer:

```sh
geminidepthport --root /private/deployment quality-session-summary cam_a \
  --generation GENERATION_32_HEX --session SESSION_32_HEX
```

HTTP: `/api/quality/session-summary?camera=cam_a&generation=GENERATION&session=SESSION`.
Both selectors are mandatory together. Omitting both retains the existing live
query. History is never returned as `current` and never authorizes a recording
gate. Every historical response declares `live: false`, `worker_ready: false`,
`session_bound: false`, and `native_gate_ack_verified: false`; these describe its
non-live query mode, not a claim that the selected process is dead.

The reader uses owner-checked directory FDs and `openat` with `O_NOFOLLOW`, bounded
regular mode-0600 files, strict duplicate-free JSON, and generation configuration
hashes. A bound session must have a valid sealed snapshot matching its instance,
session, generation, runtime configuration hash and actual SDK/observed source.
`complete: true` / `available: true` requires a finalized summary matching the
producer's final current record. Missing or partially committed finalization is
`incomplete: true` with no claimed summary. SIGKILL cannot fabricate finalization.
Restarts preserve separate session directories and snapshot IDs.

A session that failed before a snapshot was available remains queryable as
`SNAPSHOT_UNAVAILABLE`, `snapshot_verified: false`, with explicitly unbound
`facts` (quality and event-page JSON). Those facts retain unknown/new event scopes
and do not imply calibration verification, SDK frameset identity or readiness.
The tool does not infer business recording validity or physical accuracy.

`management_acceptance.py` now queries this history after normal formal `down`
and requires the original protected session's exact snapshot ID and finalized
summary. A later worker's ready status cannot satisfy this check.
