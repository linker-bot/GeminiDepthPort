#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/humble/setup.bash
if [ "$#" -gt 0 ]; then exec "$@"; fi
python3 /app/validate_runtime.py
exec /usr/local/bin/capture-service --config /config/capture.json
