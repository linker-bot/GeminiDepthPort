# Resident SDK quality adapter v1

The optional root `quality_runtime` field is a `quality_core::RuntimeQualityPolicy`.
Its nested `quality` must equal root `quality_policy` exactly, including action
and thresholds. The service rejects inconsistent, incomplete, duplicate-key or
unsupported policies. This adapter accepts **report-only** action; it does not
claim fail-closed behavior while continuing publication. Old configurations
without `quality_runtime` retain their previous behavior.

The complete runtime policy is included in the actual SDK capture snapshot's
configuration content and hash. Each worker owns a fresh acquisition session and
binds its quality evidence to the supervisor's explicit instance ID. Reopening
the SDK creates a new worker/session and reads calibration again; old counters,
source-time high-water marks and recovery streaks do not cross into that epoch.
This adapter does not change restart budgets, warmup allowances, recording
protection, pairing tolerances, queue capacity, publishing or device properties.
A report-only quality degradation is separate from the existing capture
`ready` state; inspect `status.quality.state` as well as `status.ready`.

## Observations and limits

Every present SDK color/depth frame increments that stream's received count
before complete-pair, pixel-format or pairing acceptance. Counts are independent
of published/accepted pair counts. Missing streams receive no invented frame.
Before an actual SDK snapshot exists, receipts are explicitly counted as
`pre_snapshot_received_counts`; they do not invent calibration IDs or enter
snapshot-bound rolling frame windows. The latest metadata retains original
sequence, device microseconds, SDK system microseconds, format, size, scale,
monotonic receive offset and host Unix receive time. Metadata getter failures
increment received counts and open a contract violation without invented values.

After binding, each stream's rolling window checks observed format/dimensions,
layout, depth-scale validity, source mapping/age and SDK system clock progression.
Pixel valid/zero fractions remain `None` because this adapter does not scan or
certify pixels for those metrics. Conversion/pairing/publication errors are
additional group episodes; the stream window is not advertised as an end-to-end
pixel-conversion or consumer-delivery success fraction. Source sequence and
device-time high-water diagnostics are independent of the SDK system age map.
Pair delta is observed in the explicitly configured device/system clock even
when the pair is rejected. A per-loop tick detects stale streams without relying
on new frames arriving. Startup observations are counted, not silently erased.

Source age uses only the existing explicit `sdk-system-unix` mapping: SDK system
microseconds → Unix nanoseconds, compared with independently observed host Unix
receipt time. There is no device-time-to-now guess or ROS-header substitution.
Missing/overflowing/future/old mappings become quality violations. Raw clock
values are retained for diagnosis and are not converted in the source metadata.
SDK load/open/poll failure is reported as SDK availability/disconnection evidence;
this classification does not certify a physical unplug event.

Actual whole-pair queue eviction opens `QUEUE_OVERFLOW`; successful queue
admission provides healthy evidence. The current service drains synchronously
and still uses the existing bounded whole-pair queue. Publication is wrapped at
the actual `rgbd::drain` boundary: only successfully returned bridge publish
calls increment `publication_calls`; a later failing call opens
`PUBLISH_PARTIAL_FAILURE` and increments `partial_publications` if any prior call
succeeded. Neither counter means DDS delivery. All existing drain errors remain
nonzero failures; quality reporting does not suppress them. Consecutive healthy
observations satisfy the explicit recovery hysteresis. Sustained faults update
episode counters without producing an event on every frame.

## Private machine-readable files

`status.json` adds a safe `quality` object containing state, latest event sequence
and paths under its private status directory:

- `quality/<session>/current.json`: instance/session/snapshot binding, current
  quality state and bounded rolling-window summary, received/pre-snapshot counts,
  latest original stream metadata, and publication counters.
- `quality/<session>/events.json`: bounded transition page, explicit cursor and
  `EVENT_GAP` evidence if older events were evicted. No unbounded append log.
- `quality/<session>/summary.json`: final per-session summary and receipt counts,
  written on a normal/error worker exit. A hard process kill/native hang cannot
  invent a final summary; absence remains explicit in the last status reference.

Files use the existing atomic private writer (0600 files, 0700 leaf directory,
write/sync/rename). Each file is atomic independently, not a multi-file database
transaction. Readers must check session/instance and `latest_event_sequence`
when correlating current state and events, retrying if a concurrent update is
observed. Status is written after the corresponding current/events files. Read `/tmp/capture/status.json` inside the matching capture container, then
read its referenced private quality paths. There is no new management
`capture-status` command or HTTP query endpoint in this version; these JSON
files and the Rust query API are the machine interfaces. Calibration/serial
data is not exposed in the status object. The quality files themselves
are private acquisition evidence and contain opaque identity/clock metadata.

Generated Rust configurations explicitly include all runtime limits: stale/age
budget equal to existing `idle_timeout_ms`, existing pairing delta, 100 ms future
budget, valid fraction 0 and zero fraction 1, report-only action; window capacity
1024 and 10-second horizon; transition capacity 256; one session/summary per
worker engine; all nine rules open after one violation, recover after three
healthy observations, severity degraded. These values are serialized in the
private generated config, not hidden engine defaults. They do not loosen any
capture acceptance or recovery policy.

## Verification scope

Tests exercise the actual service adapter with synthetic SDK metadata and an
actual immutable snapshot fixture, independent receipts, tick-driven stale
transitions, repeated partial-publication failures through the runtime drain
wrapper, bounded transition behavior/recovery hysteresis, and private final
files. A real capture-service subprocess with a missing SDK verifies error exit,
redacted status references and finalized zero-frame quality files. Generation
tests verify idle/pair budget retention and every explicit runtime field. These
are offline checks, not hardware or ROS delivery acceptance.
