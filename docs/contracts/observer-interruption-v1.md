# Explicit observed-session interruption

The decoded observer accepts the bounded version-1 command:

```json
{"version":1,"command":{"kind":"interrupt","at":{"session_id":"32 lowercase hex","offset_ns":1000},"reason":"driver-exited"}}
```

Reason is a fixed enum: `driver-exited`, `observer-interrupted`, or
`core-unavailable`. A caller-observed driver/process/protocol failure is not a
physical `DEVICE_DISCONNECTED` claim. SDK sequence, clock and frameset provenance
remain unknown in the observed source. No frames, pixel observations, quality
threshold breaches, or device reopen operations are invented by this command.

The quality engine emits one bounded `OBSERVER_INTERRUPTED` Group episode/event
with an optional `interruption_reason`, severity failed, and the actual session
monotonic observation time. Snapshot and SessionSummary carry the same optional
reason. Repeating the same interruption is idempotent; a different reason cannot
replace the first fact, and finalized sessions cannot be edited. ReportOnly does
not suppress or decide this lifecycle fact. Existing configurable frame rules,
capacities, thresholds and recovery hysteresis remain unchanged; the lifecycle
variant is not a new required policy rule (`FrameViolation::ALL` continues to
list configurable data rules). Generic frame-rule evaluation cannot clear this
terminal lifecycle episode.

The official adapter reports an unexpected interruption before asking the live
core to finalize. It persists each session's `interruption.json` before attempting
that protocol exchange. The record contains version, instance/session/snapshot
(nullable), generation, runtime-config SHA256, reason, observed Unix time, actual
monotonic session offset, `core_reported` and `quality_finalized`. The current and
summary wrappers retain this `lifecycle_interruption` fact. Normal successful
SIGTERM/SIGINT shutdown does not report an interruption.

If the core has already died or cannot answer, the adapter cannot fabricate a
quality event, finalized snapshot or summary on its behalf. It retains the last
real quality snapshot (or null if none was observed), adds the independent
controller interruption fact, and leaves summary unavailable/incomplete. Failure
before ROS subscription setup also persists a session-specific record. The
adapter updates global status to degraded/not-ready (normal stop: stopped),
without erasing earlier session directories when a later observation session
starts. Management/history consumers must retain `lifecycle_interruption`, even
when quality is null or the last known data-quality state was normal. This is
controller evidence, not an inferred SDK acquisition session or business-validity
decision.

Tests cover the real JSONL interruption command, final summary and event retention
with zero fabricated frames, idempotency, all three reasons, dead-core fallback,
pre-core setup failure, driver/core classification, and normal successful stop.
No hardware or physical-accuracy certification is performed; physical accuracy
flags remain false.
