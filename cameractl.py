#!/usr/bin/env python3
"""Host-side configuration and lifecycle tool. Requires Python 3.8+ and PyYAML."""
import argparse
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from types import SimpleNamespace
import time
import tempfile
from pathlib import Path
import yaml
import configuration as config
import generation
import driver_build
import lifecycle
import operation_guard as guard
from configuration import ConfigurationError, MODEL_PROFILES, validate_inventory as validate

ROOT = Path(__file__).resolve().parent
INV = ROOT / 'inventory.json'
PROJECT = 'orbbec-multicam'


@contextmanager
def control_lock():
    with guard.control_lock(ROOT): yield


def mutation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with control_lock():
            guard.assert_unprotected(ROOT)
            return function(*args, **kwargs)
    return wrapped


def controller():
    # Namespace view keeps patched public functions testable without global module registration.
    return SimpleNamespace(**globals())


def run(args, capture=False):
    timeout = 3600 if args[:2] == ['docker', 'build'] else 60
    return subprocess.run(args, cwd=ROOT, check=True, text=True, timeout=timeout,
                          stdout=subprocess.PIPE if capture else None).stdout

def load():
    return json.loads(INV.read_text())

def _atomic_inventory(data):
    payload = json.dumps(data, indent=2, allow_nan=False) + '\n'
    descriptor, filename = tempfile.mkstemp(prefix='.inventory-', suffix='.tmp', dir=INV.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, INV)
    finally:
        if os.path.exists(filename): os.unlink(filename)


@mutation
def save(data, *, _probe_confirmed=False):
    previous = load() if INV.exists() else {'cameras': []}
    candidate = config.invalidate_changed(previous, data)
    old = {camera['name']: camera for camera in previous.get('cameras', [])}
    for camera in candidate['cameras']:
        before = old.get(camera['name'], {})
        newly_confirmed = camera['profiles_verified'] and (
            not before.get('profiles_verified', False)
            or camera.get('verification') != before.get('verification'))
        config.require(not newly_confirmed or _probe_confirmed,
                       'PROFILE_PROBE_REQUIRED', 'Confirmation requires an actual runtime probe')
    validate(candidate, draft=True)
    _atomic_inventory(candidate)


def migrate_inventory(bindings=None, apply=False, expected_plan=None):
    if apply:
        with control_lock():
            guard.assert_unprotected(ROOT)
            return _migrate_inventory(bindings, apply, expected_plan)
    return _migrate_inventory(bindings, apply, expected_plan)


def _migrate_inventory(bindings=None, apply=False, expected_plan=None):
    # Caller holds control_lock, just like other CLI mutations.
    source = INV.read_bytes()
    candidate = config.migrate_legacy(json.loads(source), bindings)
    plan = config.migration_plan_digest(source, candidate)
    summary = {'schema_version': 2, 'camera_count': len(candidate['cameras']),
               'enabled_count': len(active(candidate)), 'plan_digest': plan,
               'confirmation_required': True, 'native_yuyv_required': True,
               'runtime_supported': False, 'generation_supported': True, 'applied': False}
    if not apply:
        return summary
    config.require(expected_plan == plan, 'MIGRATION_STALE', 'Preview this exact migration before applying')
    guard.assert_unprotected(ROOT)
    assert_stopped()
    config.require(INV.read_bytes() == source, 'MIGRATION_STALE', 'Inventory changed during migration')
    folder = ROOT / 'state' / 'migrations'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, _ = tempfile.mkstemp(prefix='inventory-v1-', suffix='.json', dir=folder)
    with os.fdopen(descriptor, 'wb') as backup:
        backup.write(source)
        backup.flush()
        os.fsync(backup.fileno())
    _atomic_inventory(candidate)
    return dict(summary, applied=True, backup_created=True)


def active(data):
    return [c for c in data['cameras'] if c['enabled']]


def scan():
    devices = []
    for path in sorted(Path('/sys/bus/usb/devices').glob('*')):
        def read(name):
            f = path / name
            return f.read_text().strip() if f.exists() else ''
        if read('idVendor').lower() == '2bc5':
            devices.append({'port': path.name, 'serial_number': read('serial'), 'product': read('product'),
                            'speed_mbps': read('speed'), 'bus': read('busnum'), 'device': read('devnum')})
    return devices

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def compose(*args, capture=False):
    os.environ['ORBBEC_DEPLOY_DIR'] = str(ROOT)
    snapshot = generation.load_current(ROOT)
    file = snapshot['folder'] / 'compose.yaml' if snapshot else ROOT / 'compose.yaml'
    return run(['docker', 'compose', '--project-directory', str(ROOT), '-p', PROJECT, '-f', str(file), *args], capture)


def validate_bundle(file):
    os.environ['ORBBEC_DEPLOY_DIR'] = str(ROOT)
    run(['docker', 'compose', '--profile', 'cameras', '--project-directory', str(ROOT), '-p', PROJECT,
         '-f', str(file), 'config', '--quiet'], True)


def assert_stopped():
    config.require(not any(v['State'].get('Running') for v in lifecycle.instances(controller())),
                   'CAMERAS_RUNNING', 'Stop camera services before configuration maintenance')
    assert_no_previews()


def assert_no_previews():
    config.require(not lifecycle.instances(controller(), 'preview'),
                   'PREVIEWS_RUNNING', 'Stop previews before configuration maintenance')


def web_service():
    root = '${ORBBEC_DEPLOY_DIR:?Run ./deploy.sh web or set ORBBEC_DEPLOY_DIR}'
    return {
        'image': 'local/orbbec-console:compose-v1',
        'build': {'context': '.', 'dockerfile': 'web.Dockerfile'},
        'working_dir': root,
        'user': '${DEPLOY_UID:-0}:${DEPLOY_GID:-0}', 'group_add': ['${DOCKER_GID:-0}'],
        'command': ['python3', 'web_server.py', '--host', '0.0.0.0', '--port', '8765'],
        'restart': 'unless-stopped', 'init': True, 'stop_grace_period': '30s',
        'labels': {'orbbec.role': 'management'},
        'ports': ['127.0.0.1:${WEB_PORT:-8765}:8765'],
        'environment': {'ORBBEC_CONTAINERIZED': '1', 'ORBBEC_DEPLOY_DIR': root,
                        'ORBBEC_PUBLIC_PORT': '${WEB_PORT:-8765}', 'PYTHONUNBUFFERED': '1',
                        'HOME': '/tmp/console-home', 'DOCKER_CONFIG': '/tmp/docker', 'XDG_CACHE_HOME': '/tmp/cache'},
        'volumes': [
            {'type': 'bind', 'source': root, 'target': root},
            {'type': 'bind', 'source': '/var/run/docker.sock', 'target': '/var/run/docker.sock'},
            {'type': 'bind', 'source': '/sys', 'target': '/sys', 'read_only': True},
            {'type': 'bind', 'source': '/dev/bus/usb', 'target': '/dev/bus/usb', 'read_only': True},
            {'type': 'bind', 'source': '/run/udev', 'target': '/run/udev', 'read_only': True},
            {'type': 'bind', 'source': '/etc/udev/rules.d', 'target': '/etc/udev/rules.d', 'read_only': True}],
        'read_only': True, 'tmpfs': ['/tmp:size=128m'],
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'healthcheck': {'test': ['CMD', 'python3', '-c',
            "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8765/healthz', timeout=3)"],
            'interval': '10s', 'timeout': '5s', 'retries': 6, 'start_period': '10s'},
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}

def camera_service(data, env, folder):
    return {
        'image': data['image'],
        'build': {'context': str(ROOT), 'args': {'ORBBEC_COMMIT': data['upstream_commit'],
                                               'YUYV_PATCH_SHA256': driver_build.patch_identity()['patch_sha256']}},
        'network_mode': 'host', 'ipc': 'private', 'init': True,
        'user': str(os.getuid()) + ':' + str(os.getgid()),
        'profiles': ['cameras'], 'labels': {'orbbec.role': 'camera'},
        'restart': 'no', 'stop_signal': 'SIGINT', 'stop_grace_period': '30s',
        'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'shm_size': '256m', 'tmpfs': ['/tmp:size=256m'],
        'environment': dict(env, HOME='/tmp', ROS_HOME='/tmp/ros'),
        'volumes': [{'type': 'bind', 'source': str(folder), 'target': '/config', 'read_only': True},
                    '/dev/bus/usb:/dev/bus/usb:rw', '/run/udev:/run/udev:ro'],
        'device_cgroup_rules': ['c 189:* rwm'],
        'healthcheck': {'test': ['CMD', '/app/entrypoint.sh', 'python3', '/app/health.py'],
                        'interval': '30s', 'timeout': '25s', 'start_period': '45s', 'retries': 3},
        'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}


@mutation
def render(data, draft=False):
    return generation.create(controller(), data, draft)


def check_render(data):
    validate(data)
    current = generation.load_current(ROOT)
    config.require(current is not None and not current['draft'], 'GENERATION_DRAFT', 'Generate a verified deployment first')
    config.require(current['inventory_sha256'] == digest(INV) and load() == data,
                   'INVENTORY_DRIFT', 'Saved inventory differs from active generation')
    config.require(bool(current['cameras']), 'NO_ACTIVE_CAMERAS', 'No required camera streams')
    return current


def preflight(data):
    assert_no_previews()
    check_render(data)
    config.require(platform.system() == 'Linux', 'PLATFORM_UNSUPPORTED', 'Real deployment requires native Linux')
    config.require(Path('/dev/bus/usb').is_dir() and Path('/run/udev').is_dir(),
                   'DEVICE_ACCESS', 'Required device access unavailable')
    config.require(Path('/etc/udev/rules.d/99-obsensor-libusb.rules').is_file()
                   or Path('/lib/udev/rules.d/99-obsensor-libusb.rules').is_file(),
                   'DEVICE_ACCESS', 'Required host device rules unavailable')
    found = scan()
    for camera in active(data):
        config.require(sum(d['serial_number'] == camera['serial_number'] for d in found) == 1,
                       'DEVICE_UNAVAILABLE', 'Required device binding is unavailable')
    run(['docker', 'info'], True)
    compose('config', '--quiet', capture=True)
    image = json.loads(run(['docker', 'image', 'inspect', data['image']], True))[0]
    labels = image['Config'].get('Labels') or {}
    config.require(labels.get('org.opencontainers.image.revision') == data['upstream_commit'],
                   'IMAGE_REVISION', 'Driver image revision does not match inventory')
    if data['schema_version'] == 2:
        config.require(labels.get('org.geminidepthport.explicit-bindings') == '1'
                       and labels.get('org.geminidepthport.native-yuyv') == '1'
                       and labels.get('org.geminidepthport.native-yuyv-patch') == driver_build.patch_identity()['patch_sha256']
                       and labels.get('org.geminidepthport.communication-profile') == generation.COMMUNICATION_PROFILE,
                       'IMAGE_CONTRACT_UNSUPPORTED', 'Driver image does not declare the required public interface')


def healthy(service, timeout):
    return lifecycle.wait_healthy(controller(), service, timeout)


@mutation
def stop_cameras(remove=False):
    return lifecycle.stop(controller(), remove=remove)


@mutation
def stop_previews():
    return lifecycle.stop(controller(), kind='preview', remove=True)


@mutation
def stop_stack(remove=False):
    # Keep the management process alive if any device could not be stopped.
    stop_previews()
    stop_cameras(remove=remove)
    return lifecycle.stop(controller(), kind='management', remove=remove)


@mutation
def start_cameras(data):
    return lifecycle.start(controller(), data)


@mutation
def ensure_started(data):
    return lifecycle.ensure_started(controller(), data)


def acquire_recording_guard():
    with control_lock():
        guard.assert_unprotected(ROOT)
        data = load(); snapshot = check_render(data)
        existing = lifecycle.check_existing(controller(), snapshot)
        config.require(len(existing) == len(snapshot['cameras']), 'GROUP_INCOMPLETE', 'Required cameras are not running')
        for camera in snapshot['cameras']:
            healthy(camera['service'], data['startup_timeout_seconds'])
            lifecycle.probe(controller(), camera['service'])
        return guard.acquire(ROOT, snapshot['generation'])


def release_recording_guard(token):
    with control_lock(): guard.release(ROOT, token)


@mutation
def build_image(data):
    validate(data, draft=True)
    run(['docker', 'build', '--build-arg', 'ORBBEC_COMMIT=' + data['upstream_commit'],
         '--build-arg', 'YUYV_PATCH_SHA256=' + driver_build.patch_identity()['patch_sha256'],
         '-t', data['image'], '.'], True)


def export_calibration():
    """Read running ROS evidence under the shared lock; never open a device."""
    import calibration
    with control_lock():
        data = load()
        snapshot = check_render(data)
        existing = lifecycle.check_existing(controller(), snapshot)
        config.require(len(existing) == len(snapshot['cameras']), 'GROUP_INCOMPLETE',
                       'Calibration requires every active camera')
        for camera in snapshot['cameras']:
            lifecycle.probe(controller(), camera['service'])
        request = calibration.capture_request(data, snapshot['generation'])
        # One running container subscribes to all publishers in the shared DDS domain.
        container = existing[snapshot['cameras'][0]['name']]['Id']
        remote = '/tmp/calibration-' + __import__('secrets').token_hex(16) + '.json'
        try:
            result = subprocess.run(['docker', 'exec', '-i', container, '/app/entrypoint.sh',
                'python3', '/app/calibration_export.py', '--request', '-', '--output', remote],
                input=json.dumps(request), capture_output=True, text=True, timeout=140, check=False)
            config.require(result.returncode == 0, 'CALIBRATION_COLLECTION_FAILED',
                           'Published calibration evidence could not be collected')
            evidence = json.loads(run(['docker', 'exec', container, 'cat', remote], True))
            current = check_render(load())
            config.require(current['generation'] == snapshot['generation'] and load() == data,
                           'CALIBRATION_BINDING', 'Configuration changed during collection')
            config.require(len(lifecycle.check_existing(controller(), current)) == len(current['cameras']),
                           'GROUP_INCOMPLETE', 'Camera group changed during collection')
            record = calibration.build_snapshot(data, snapshot['generation'], evidence)
            return calibration.save_snapshot(ROOT, record, data, snapshot['generation'])
        finally:
            run(['docker', 'exec', container, 'rm', '-f', remote], True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('scan')
    v = sub.add_parser('validate')
    v.add_argument('--file', type=Path)
    v.add_argument('--draft', action='store_true')
    m = sub.add_parser('migrate')
    m.add_argument('--bindings', type=Path)
    m.add_argument('--apply', action='store_true')
    m.add_argument('--expected-plan')
    r = sub.add_parser('render'); r.add_argument('--draft', action='store_true')
    for cmd in ('preflight', 'build', 'up', 'status', 'check', 'down', 'devices', 'image-ref', 'protect', 'release-protection', 'logs', 'calibration-export'):
        sub.add_parser(cmd)
    stop = sub.add_parser('stack-stop'); stop.add_argument('--remove', action='store_true')
    b = sub.add_parser('bind'); b.add_argument('name'); b.add_argument('serial')
    s = sub.add_parser('set'); s.add_argument('name'); s.add_argument('key'); s.add_argument('json_value')
    v = sub.add_parser('confirm-profile'); v.add_argument('name')
    a = parser.parse_args()
    if a.command == 'calibration-export':
        print(json.dumps(export_calibration())); return
    if a.command == 'stack-stop':
        print(json.dumps(stop_stack(remove=a.remove))); return
    if a.command == 'down':
        print(json.dumps(stop_cameras())); return
    if a.command == 'protect':
        print(json.dumps({'token': acquire_recording_guard()})); return
    if a.command == 'release-protection':
        release_recording_guard(sys.stdin.read().strip()); return
    if a.command == 'scan':
        config.require(platform.system() == 'Linux', 'PLATFORM_UNSUPPORTED', 'Device discovery requires Linux')
        print(json.dumps(scan(), indent=2, ensure_ascii=False)); return
    if a.command == 'migrate':
        bindings = json.loads(a.bindings.read_text()) if a.bindings else None
        print(json.dumps(migrate_inventory(bindings, a.apply, a.expected_plan)))
        return
    data = json.loads(a.file.read_text()) if a.command == 'validate' and a.file else load()
    if a.command in ('bind', 'set', 'confirm-profile'):
        guard.assert_unprotected(ROOT)
        c = next((c for c in data['cameras'] if c['name'] == a.name), None)
        config.require(c is not None, 'CAMERA_ID', 'Unknown logical camera identity')
        if a.command == 'bind':
            c['serial_number'] = a.serial
        elif a.command == 'set':
            c['parameters'][a.key] = json.loads(a.json_value)
        else:
            from web_server import Manager
            Manager().confirm(a.name)
            print('Actual profile probe passed.'); return
        save(data); print('Inventory updated; stop and render before deployment.'); return
    if a.command == 'image-ref':
        validate(data, draft=True); print(data['image'])
    elif a.command == 'render':
        result = render(data, a.draft); print(json.dumps({'generated': True, 'draft': result['draft'], 'camera_count': len(result['cameras'])}))
    elif a.command == 'validate':
        validate(data, draft=a.draft)
        print(json.dumps({'valid': True, 'schema_version': data['schema_version'],
                          'enabled_count': len(active(data)), 'hardware_verified': False}))
    elif a.command == 'build': build_image(data)
    elif a.command == 'devices':
        guard.assert_unprotected(ROOT)
        assert_stopped()
        run(['docker', 'run', '--rm', '--network', 'host', '--cap-drop', 'ALL',
             '--device-cgroup-rule', 'c 189:* rwm', '-v', '/dev/bus/usb:/dev/bus/usb:rw',
             '-v', '/run/udev:/run/udev:ro', data['image'], 'ros2', 'run', 'orbbec_camera', 'list_devices_node'])
    elif a.command == 'preflight': preflight(data)
    elif a.command == 'up': print(json.dumps(ensure_started(data)))
    elif a.command == 'check':
        snapshot = check_render(data)
        for camera in snapshot['cameras']: lifecycle.probe(controller(), camera['service'])
    elif a.command == 'status': compose('ps', '-a')
    elif a.command == 'logs':
        for item in lifecycle.instances(controller()):
            run(['docker', 'logs', '--tail', '100', item['Id']])

if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['image-ref']:
            main()  # Read-only helper also used while the host holds the shared lock.
        else:
            with control_lock(): main()
    except (AssertionError, RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print('ERROR:', str(exc) if isinstance(exc, ConfigurationError) else 'Operation failed; inspect private diagnostics', file=sys.stderr)
        raise SystemExit(1)
