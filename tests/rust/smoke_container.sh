#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
IMAGE=${1:-geminidepthport-web:ci}
TEMP_ROOT=$(mktemp -d)
CONTAINER=
cleanup() {
  if [[ -n "$CONTAINER" ]]; then docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
  rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT
cp "$ROOT/inventory.v2.example.json" "$TEMP_ROOT/inventory.json"
cp -R "$ROOT/web" "$TEMP_ROOT/web"
PUBLIC_PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
CONTAINER=$(docker run -d --read-only --cap-drop ALL --tmpfs /tmp \
  --user "$(id -u):$(id -g)" -v "$TEMP_ROOT:$TEMP_ROOT:rw" -w "$TEMP_ROOT" \
  -e ORBBEC_CONTAINERIZED=1 -e ORBBEC_DEPLOY_DIR="$TEMP_ROOT" -e ORBBEC_PUBLIC_PORT="$PUBLIC_PORT" \
  -e HOME=/tmp/home -e DOCKER_CONFIG=/tmp/docker \
  -p "127.0.0.1:$PUBLIC_PORT:8765" "$IMAGE" geminidepthport serve --host 0.0.0.0)
PORT=$(docker port "$CONTAINER" 8765/tcp | head -1)
python3 "$ROOT/tests/smoke_web_container.py" --url "http://$PORT" --root "$TEMP_ROOT"
docker exec "$CONTAINER" geminidepthport http-health
# Prove management does not depend on a Python interpreter.
docker exec "$CONTAINER" sh -c '! command -v python3'
