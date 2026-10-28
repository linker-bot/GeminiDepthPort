#!/usr/bin/env bash
# No device mounts, SDK loads, private config or real calibration.
set -euo pipefail
image=${1:-geminidepthport-ros-bridge:ci}
check_old=${2:-}
[[ -z "$check_old" || "$check_old" == --check-old ]] || exit 2
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
sub="gdp-dds-control-sub-$$"
pub="gdp-dds-control-pub-$$"
cleanup() {
  docker rm -f "$sub" "$pub" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT INT TERM
run_control() {
  local localhost_only=$1
  local expected=$2
  local args=(--network host --ipc private --read-only --cap-drop ALL
    --tmpfs /tmp:rw,nosuid,nodev,size=64m
    --mount "type=bind,src=$root/ros/tests/cross_container_control.py,dst=/control.py,readonly"
    --mount "type=bind,src=$root/runtime/fastdds.xml,dst=/control.xml,readonly"
    -e HOME=/tmp -e ROS_LOG_DIR=/tmp/ros-logs -e ROS_DOMAIN_ID=47
    -e "ROS_LOCALHOST_ONLY=$localhost_only" -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp
    -e FASTRTPS_DEFAULT_PROFILES_FILE=/control.xml
    -e "GDP_DDS_CONTROL_RUN=run_$$_${localhost_only}")
  local subscriber=(sub)
  if [[ "$expected" == none ]]; then subscriber+=(--expect-no-data); fi
  docker run -d --name "$sub" "${args[@]}" "$image" bash -lc \
    'source /opt/ros/humble/setup.bash; exec python3 /control.py "$@"' -- "${subscriber[@]}" >/dev/null
  sleep 0.5
  local pub_exit=0 sub_exit
  timeout 25s docker run --name "$pub" "${args[@]}" "$image" bash -lc \
    'source /opt/ros/humble/setup.bash; exec python3 /control.py pub' >"$work/pub.out" 2>&1 || pub_exit=$?
  sub_exit=$(timeout 25s docker wait "$sub")
  docker logs "$sub" >"$work/sub.out" 2>&1
  cat "$work/pub.out" "$work/sub.out"
  [[ "$pub_exit" == 0 && "$sub_exit" == 0 ]]
  docker rm "$sub" "$pub" >/dev/null
}
run_control 0 all
if [[ "$check_old" == --check-old ]]; then
  # Optional regression reproduction: Humble localhost mode injects SHM despite
  # the XML profile, so isolated-IPC containers can discover but receive no data.
  run_control 1 none
fi
