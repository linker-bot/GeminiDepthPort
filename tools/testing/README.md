# Explicit multi-camera test runner

`multicamera_runner.py` is an opt-in, bounded experiment tool. It permits a
1–8 camera **test plan**, including a four-camera synthetic lifecycle test. It
never changes the formal production camera limit of three, calls the production
manager's apply/set/start operations, certifies capacity, or schedules a scale,
performance or long-duration test. No hardware execution is authorized merely
by generating a plan.

## Plan without Docker

```sh
python3 tools/testing/multicamera_runner.py --manifest /absolute/private/test.json
```

The manifest has exactly `version:1`, integer `seconds` (1–300), explicit integer
`usbfs_min_mb` (1–65536), and `cameras`. Each camera has a unique logical `id`, an
`image` reference and absolute `capture_config`, `runtime_manifest` and
`consumer_spec` file paths. Use canonical absolute paths without symlink
components. Every input must be a regular file owned by the executing user,
mode 0600, with one hard link and size no greater than 4 MiB. Nested duplicate
JSON keys and nonfinite numbers are rejected before hashing.

The plan checks unique device selections, non-overlapping ROS namespaces,
unique optical frame IDs, one common generation, profile/consumer topic/frame
binding, explicit zero automatic restarts, quality/pair policy agreement, and
runtime hash agreement. Multiple cameras may share one immutable runtime/image;
their runtime hashes are bound, not required to be different. The public plan
contains logical IDs, image references, dimensions/rates and input digests. It
excludes serials, private paths, frame IDs and calibration matrices. Preflight
opens no SDK, contacts no Docker daemon and changes no USBFS/management state.
It cannot certify that an image or physical device is available.

## Execution requires a separate explicit invocation

```sh
sudo python3 tools/testing/multicamera_runner.py \
  --manifest /absolute/private/test.json --execute \
  --confirm-plan EXACT_PLAN_SHA256 --output /absolute/private/new-result \
  --management-root /absolute/private/production-root
```

The CLI requires a freshly recomputed exact plan digest, a new output directory,
root execution, a local Docker socket at `unix:///var/run/docker.sock`, and a
non-rootless daemon. Remote `DOCKER_HOST` settings are rejected. The immutable
image ID is resolved and used for containers. Inputs are re-read and compared to
the planned bytes immediately before use. Runtime manifests and the capture
entrypoint continue to enforce SDK/bridge/service artifact identity.

Execution takes the same `state/control.lock` inode and `flock(LOCK_EX|LOCK_NB)`
protocol as production management. It rejects symlink/nonregular/hard-linked or
improperly owned/permitted lock files, refuses an existing recording guard even
if malformed or dangling, and rechecks guards during startup, observation and
consumer drain. It never removes a guard or retries a production operation.
The lock remains held through cleanup. Test-only `FakeDocker` injection is a
Python API for deterministic tests; no CLI flag exposes it or bypasses root.

Capture containers have private IPC, a read-only filesystem and bounded logs.
Each container is labelled with one fresh random test-run ID. Current actual
snapshot geometry, session/generation and runtime config digest must match the
consumer spec before measurement begins. Consumer containers run the unchanged
strict RGBD acceptance observer. Startup and consumer drain are outside the
requested observation window, and are bounded separately. The runner refuses
session changes/non-ready state, consumer contract failures and nonzero consumer
exit status. It does not rewrite acceptance thresholds.

Cleanup discovers run-labelled containers even if `docker run` created one
before the client timed out. It stops only expected names whose inspected label
matches this run, in reverse creation order; it never stops an unrelated
container. It retains stopped containers/logs for inspection. Cleanup failure
writes `CLEANUP_REQUIRED` and forces failure. Raw statuses/logs and calibration
are private in the mode-0700 output tree. Stdout contains only the redacted
summary or stable error code.

`result.json` distinguishes simulated execution, acceptance, observation errors,
consumer outcomes and cleanup. `COMPLETE` is written only if all acceptance and
cleanup checks succeed. A failed producer, failed consumer, changed session,
guard, timeout, client error or cleanup error exits nonzero and cannot leave a
successful completion marker. A successful *simulated* run remains explicitly
`simulated:true` and is never hardware/capacity evidence.

## Deterministic tests

```sh
python3 -m unittest discover -s tests -p 'test_multicamera_runner.py'
```

These tests inject FakeDocker/FakeClock, cover a four-camera lifecycle and
failure paths, and do not run Docker, devices, SSH, production management,
performance measurements or scale trials. They cover strict/private input,
identity/namespace/hash mismatches, plan confirmation, rootless/remote refusal,
current snapshot mismatch, daemon-create/client-failure cleanup, consumer exit
failure, owner-isolated cleanup, production lock contention/guard refusal, and
mid-window session/guard changes. The production three-camera code is untouched.
