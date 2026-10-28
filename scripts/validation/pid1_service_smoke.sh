#!/usr/bin/env bash
# Exercise the actual service as Docker PID 1, without SDK or USB access.
set -euo pipefail
image=${1:?Usage: bash scripts/validation/pid1_service_smoke.sh IMAGE}
work=$(mktemp -d)
control_id="${work##*/}"
container="gdp-pid1-service-smoke-$$-$control_id"
cleanup() {
  if [[ "$(docker inspect -f '{{ index .Config.Labels "org.geminidepthport.control-id" }}' "$container" 2>/dev/null || true)" == "$control_id" ]]; then
    docker rm -f "$container" >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT INT TERM
mkdir "$work/output"
python3 - "$work/capture.json" <<'PY'
import json, pathlib, sys
config = dict(
    version=1,
    sdk_library="/gdp-pid1-control/nonexistent/libOrbbecSDK.so",
    serial="synthetic-pid1-no-device",
    color=dict(width=640, height=480, fps=30, frame_id="synthetic_color_optical"),
    depth=dict(width=640, height=480, fps=30, frame_id="synthetic_depth_optical", unit_policy="normalize-mm"),
    pairing=dict(clock="device", max_delta_us=20000),
    poll_ms=100,
    capacity=2,
    ros=dict(bridge="/opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so",
             namespace="/gdp_pid1_control", stamp_source="sdk-system-unix"),
    service=dict(status_path="/out/status.json", generation="synthetic-pid1-control",
                 idle_timeout_ms=1000, startup_timeout_ms=5000, heartbeat_ms=100,
                 heartbeat_timeout_ms=2000, max_restarts=0, restart_backoff_ms=100,
                 shutdown_timeout_ms=500, ready_min_pairs=2),
)
pathlib.Path(sys.argv[1]).write_text(json.dumps(config))
PY
exit_code=0
# No --init: capture-service must itself be PID 1. The nonexistent SDK path
# fails dlopen before Context/device construction; no device mounts are supplied.
timeout 15s docker run --name "$container" --label "org.geminidepthport.control-id=$control_id" --network none --ipc private \
  --read-only --cap-drop ALL --tmpfs /tmp:rw,nosuid,nodev,size=32m \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$work/capture.json,dst=/config/capture.json,readonly" \
  --mount "type=bind,src=$work/output,dst=/out" \
  --entrypoint /usr/local/bin/capture-service \
  "$image" --config /config/capture.json >"$work/container.log" 2>&1 || exit_code=$?
python3 - "$work/output/status.json" "$exit_code" <<'PY'
import json, pathlib, re, sys
checks = dict(expected_missing_sdk_exit=sys.argv[2] == "1", status_present=False,
              worker_session_initialized=False, supervisor_is_pid_one=False,
              not_ready=False, degraded=False)
try:
    status = json.loads(pathlib.Path(sys.argv[1]).read_text())
    checks.update(status_present=True,
                  worker_session_initialized=bool(re.fullmatch(r"[0-9a-f]{32}", status.get("session_id", ""))),
                  supervisor_is_pid_one=status.get("supervisor_pid") == 1,
                  not_ready=status.get("ready") is False,
                  degraded=status.get("state") == "degraded")
except (OSError, ValueError, TypeError):
    pass
ok = all(checks.values())
print(json.dumps(dict(ok=ok, **checks)))
raise SystemExit(0 if ok else 1)
PY
