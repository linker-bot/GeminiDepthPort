#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT=orbbec-multicam
UNIT=orbbec-stack.service
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'HELP'
Usage: ./deploy.sh COMMAND
  install              Build images, install udev rules, enable/start systemd (sudo)
  web                  Build/start only the Web console for commissioning
  up                   Start Web, then ensure all enabled cameras are ready
  boot                 systemd entry; unconfigured cameras leave Web available
  stop                 Stop Web, previews and cameras; keep containers/config
  down                 Stop and remove this Compose stack; keep persistent files
  status               Show managed service states
  logs                 Show recent managed camera logs
  enable-autostart     Enable/start the systemd boot service (sudo)
  disable-autostart    Disable/stop the boot service (sudo)
HELP
}
initialize() {
  [[ $(uname -s) == Linux ]] || fail 'Deployment requires native Linux.'
  case "$ROOT" in
    *$'\n'*|*\"*|*\'*|*\\*|*'$'*|*'#'*|*'%'*|*'&'*|*'|'*) fail 'Use a stable deployment path without quotes, newlines, $, #, %, &, | or backslashes.' ;;
  esac
  command -v docker >/dev/null || fail 'Install Docker Engine and the Compose plugin first.'
  docker compose version >/dev/null
  docker info >/dev/null
  [[ -S /var/run/docker.sock ]] || fail 'Local rootful /var/run/docker.sock is required.'
  endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' "$(docker context show)")}
  [[ "$endpoint" == unix:///var/run/docker.sock || "$endpoint" == unix:///run/docker.sock ]] || fail 'Remote/rootless Docker contexts are unsupported: host bind paths must refer to this Linux machine.'
  export ORBBEC_DEPLOY_DIR="$ROOT"
  if [[ -z ${WEB_PORT:-} && -f "$ROOT/.env" ]]; then
    while IFS='=' read -r key value; do
      if [[ "$key" == WEB_PORT && "$value" =~ ^[0-9]+$ ]]; then WEB_PORT=$value; fi
    done < "$ROOT/.env"
  fi
  export WEB_PORT=${WEB_PORT:-8765}
  [[ "$WEB_PORT" =~ ^[0-9]+$ ]] || fail 'WEB_PORT must be numeric.'
  (( 10#$WEB_PORT >= 1024 && 10#$WEB_PORT <= 65535 )) || fail 'WEB_PORT must be 1024..65535.'
  export WEB_PORT=$((10#$WEB_PORT))
  export DEPLOY_UID
  DEPLOY_UID=$(stat -c %u "$ROOT")
  export DEPLOY_GID
  DEPLOY_GID=$(stat -c %g "$ROOT")
  export DOCKER_GID
  DOCKER_GID=$(stat -c %g /var/run/docker.sock)
  if [[ "$command" == status || "$command" == logs ]]; then return; fi
  mkdir -p "$ROOT/state" "$ROOT/work"
  stack_lock
  printf "ORBBEC_DEPLOY_DIR='%s'\nWEB_PORT=%s\nDEPLOY_UID=%s\nDEPLOY_GID=%s\nDOCKER_GID=%s\n" "$ROOT" "$WEB_PORT" "$DEPLOY_UID" "$DEPLOY_GID" "$DOCKER_GID" > "$ROOT/.env.tmp"
  mv "$ROOT/.env.tmp" "$ROOT/.env"
  if [[ $EUID == 0 ]]; then
    chown -R "$DEPLOY_UID:$DEPLOY_GID" "$ROOT/state" "$ROOT/work" "$ROOT/config"
    chown "$DEPLOY_UID:$DEPLOY_GID" "$ROOT/.env" "$ROOT/inventory.json" "$ROOT/compose.yaml"
  fi
  stack_unlock
}
compose() { docker compose --project-directory "$ROOT" --env-file "$ROOT/.env" -p "$PROJECT" -f "$ROOT/compose.yaml" "$@"; }
web_ready() {
  [[ -d /dev/bus/usb && -d /run/udev && -d /etc/udev/rules.d ]] || fail 'USB/udev directories missing; run sudo ./deploy.sh install on the Linux camera host.'
  compose up -d --no-deps --no-build --wait --wait-timeout 90 web
  printf 'Web console: http://127.0.0.1:%s\n' "$WEB_PORT"
}
stack_lock() {
  command -v flock >/dev/null || fail 'Install flock for lifecycle serialization.'
  exec 9>"$ROOT/state/control.lock"
  flock -n 9 || fail 'Another lifecycle operation is in progress.'
  [[ ! -e "$ROOT/state/recording-guard.json" && ! -L "$ROOT/state/recording-guard.json" ]] || fail 'Recording protection blocks maintenance.'
}
stack_unlock() { flock -u 9; exec 9>&-; }
install_rules() {
  [[ $EUID == 0 ]] || fail 'udev installation requires sudo.'
  [[ -z $(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter label=orbbec.role=camera) ]] || fail 'Stop cameras before host maintenance.'
  [[ -z $(docker ps -q --filter "label=orbbec.web.preview=$PROJECT") ]] || fail 'Stop previews before host maintenance.'
  mkdir -p /etc/udev/rules.d
  local temporary image
  temporary=$(mktemp "$ROOT/state/udev.XXXXXX")
  image=$(compose exec -T web python3 cameractl.py image-ref)
  if ! docker run --rm --network none --cap-drop ALL --entrypoint /bin/cat "$image" /opt/host-udev/99-obsensor-libusb.rules > "$temporary"; then
    rm -f "$temporary"; fail 'Failed to extract device rules from camera image.'
  fi
  [[ -s "$temporary" ]] || fail 'Camera image exported empty device rules.'
  install -m 0644 "$temporary" /etc/udev/rules.d/99-obsensor-libusb.rules
  rm -f "$temporary"
  udevadm control --reload-rules
  udevadm trigger
}
enable_autostart() {
  [[ $EUID == 0 ]] || fail 'Enabling boot startup requires sudo.'
  command -v systemctl >/dev/null || fail 'systemd is required for boot startup.'
  [[ -d /run/systemd/system ]] || fail 'systemd is not the active init system.'
  stack_lock
  sed "s|@DEPLOY_DIR@|$ROOT|g" "$ROOT/systemd/orbbec-stack.service.in" > "$ROOT/state/orbbec-stack.service"
  install -m 0644 "$ROOT/state/orbbec-stack.service" /etc/systemd/system/orbbec-stack.service
  systemctl daemon-reload
  stack_unlock
  systemctl enable --now "$UNIT"
}
stop_stack() {
  # A one-off management helper shares the Python lock and interlock. The running
  # management container remains alive until camera/preview shutdown succeeds.
  compose run --rm --no-deps -T web python3 cameractl.py stack-stop "$@"
}
command=${1:-help}
case "$command" in help|-h|--help) usage; exit 0;; esac
initialize
case "$command" in
  install)
    [[ $EUID == 0 ]] || fail 'Run sudo ./deploy.sh install.'
    command -v udevadm >/dev/null || fail 'Install udev on the host first.'
    command -v systemctl >/dev/null || fail 'systemd is required for boot startup.'
    [[ -d /run/systemd/system ]] || fail 'systemd must be the active init system.'
    stack_lock
    compose build web
    web_ready
    stack_unlock
    compose exec -T web python3 cameractl.py build
    stack_lock
    install_rules
    stack_unlock
    enable_autostart
    printf 'Boot startup enabled. If cameras are not bound, configure and verify them in Web, then run ./deploy.sh up.\n'
    ;;
  web)
    stack_lock
    compose build web
    web_ready
    stack_unlock
    ;;
  up|boot)
    stack_lock
    web_ready
    stack_unlock
    compose exec -T web python3 deployment_control.py "$command"
    ;;
  stop) stop_stack;;
  down) stop_stack --remove;;
  status) docker ps -a --filter "label=com.docker.compose.project=$PROJECT";;
  logs) compose run --rm --no-deps -T web python3 cameractl.py logs;;
  enable-autostart) enable_autostart;;
  disable-autostart)
    [[ $EUID == 0 ]] || fail 'Run with sudo to disable systemd startup.'
    stop_stack
    systemctl disable "$UNIT"
    ;;
  *) usage; fail "Unknown command: $command";;
esac
