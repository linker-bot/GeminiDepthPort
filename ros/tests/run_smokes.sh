#!/usr/bin/env bash
set -euo pipefail
python3 /ws/ros/tests/smoke_ros.py \
  --library /opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so \
  --namespace /gdp_synthetic_smoke
exec python3 /ws/ros/tests/smoke_aux.py \
  --library /opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so \
  --namespace /gdp_synthetic_aux_smoke
