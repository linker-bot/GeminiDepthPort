#!/usr/bin/env bash
set -e
source /opt/ros/humble/setup.bash
source /ws/install/setup.bash
if [ "$#" -gt 0 ]; then exec "$@"; fi
exec python3 /app/launch_camera.py
