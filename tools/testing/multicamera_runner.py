#!/usr/bin/env python3
"""Explicit bounded multi-camera experiment; never changes the production camera limit."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.rgbd_acceptance import validate_spec
from tools.validation.make_observer_spec import rotation_quaternion

LIMIT = 4 * 1024 * 1024
LABEL = 'org.geminidepthport.test-run'
TOKEN = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9/_.:@-]{0,255}$')


class TestError(Exception):
    pass


def require(ok, code):
    if not ok:
        raise TestError(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'JSON_DUPLICATE_KEY')
        result[key] = value
    return result


def parse(raw):
    require(len(raw) <= LIMIT, 'INPUT_LIMIT')
    def invalid(_):
        raise TestError('JSON_NONFINITE')
    def finite_float(text):
        value = float(text)
        require(math.isfinite(value), 'JSON_NONFINITE')
        return value
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid, parse_float=finite_float)


def open_no_follow(path, flags, mode=0o600):
    """Walk every directory component with openat/O_NOFOLLOW, including parents."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'ABSOLUTE_INPUT_REQUIRED')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, flags | os.O_NOFOLLOW, mode, dir_fd=directory)
    finally:
        os.close(directory)


def private_read(path):
    fd = open_no_follow(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= LIMIT, 'PRIVATE_REGULAR_INPUT_REQUIRED')
        raw = source.read(LIMIT + 1)
    require(len(raw) <= LIMIT, 'INPUT_LIMIT')
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def plan(path):
    raw = private_read(path)
    value = parse(raw)
    require(set(value) == {'version', 'cameras', 'seconds', 'usbfs_min_mb'}, 'TEST_GROUP_FIELDS')
    require(type(value['version']) is int and value['version'] == 1, 'TEST_GROUP_VERSION')
    require(type(value['seconds']) is int and 1 <= value['seconds'] <= 300, 'WINDOW_LIMIT')
    require(isinstance(value['cameras'], list) and 1 <= len(value['cameras']) <= 8, 'TEST_GROUP_COUNT')
    require(type(value['usbfs_min_mb']) is int and 1 <= value['usbfs_min_mb'] <= 65536, 'USBFS_BUDGET_INVALID')
    names, serials, namespaces, frames, generations = set(), set(), set(), set(), set()
    rows, public = [], []
    for camera in value['cameras']:
        require(set(camera) == {'id', 'image', 'capture_config', 'runtime_manifest', 'consumer_spec'}, 'TEST_CAMERA_FIELDS')
        name = camera['id']
        require(isinstance(name, str) and TOKEN.fullmatch(name) and name not in names, 'TEST_CAMERA_ID')
        require(isinstance(camera['image'], str) and IMAGE.fullmatch(camera['image']), 'IMAGE_REFERENCE')
        names.add(name)
        inputs = {key: private_read(camera[key]) for key in ('capture_config', 'runtime_manifest', 'consumer_spec')}
        config, runtime, spec = (parse(inputs[key]) for key in ('capture_config', 'runtime_manifest', 'consumer_spec'))
        validate_spec(spec)
        serial = config.get('serial')
        require(isinstance(serial, str) and serial and serial not in serials, 'DEVICE_BINDING_DUPLICATE_OR_MISSING')
        serials.add(serial)
        ns = config['ros']['namespace']
        require(isinstance(ns, str) and re.fullmatch(r'/[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*', ns), 'NAMESPACE_INVALID')
        require(not any(ns == old or ns.startswith(old + '/') or old.startswith(ns + '/') for old in namespaces), 'NAMESPACE_OVERLAP')
        namespaces.add(ns)
        generation = config['service']['generation']
        require(isinstance(generation, str) and re.fullmatch('[0-9a-f]{32}', generation), 'GENERATION_ID')
        generations.add(generation)
        require(config['service']['status_path'] == '/tmp/capture/status.json'
                and config['service']['recording_control_path'] == '/recording/control.json'
                and type(config['service']['max_restarts']) is int and config['service']['max_restarts'] == 0
                and type(config['service']['startup_timeout_ms']) is int
                and 1000 <= config['service']['startup_timeout_ms'] <= 120000, 'EXPLICIT_TEST_SERVICE_POLICY')
        require(config['pairing']['max_delta_us'] == config['quality_policy']['max_pair_delta_us']
                and config['quality_runtime']['quality'] == config['quality_policy'], 'QUALITY_POLICY_MISMATCH')
        require(all(isinstance(runtime.get(key), str) and re.fullmatch("[0-9a-f]{64}", runtime[key]) for key in ("sdk_sha256", "bridge_sha256", "service_sha256")), "RUNTIME_HASH_INVALID")
        require(config['runtime_contract'] == {key: runtime[key] for key in ('sdk_sha256', 'bridge_sha256', 'service_sha256')}, 'RUNTIME_IDENTITY_MISMATCH')
        for role in ('color', 'depth'):
            stream, observed = config[role], spec['streams'][role]
            require(all(type(stream[key]) is int and stream[key] > 0 for key in ('width', 'height', 'fps')), 'PROFILE_INVALID')
            frame = stream['frame_id']
            require(frame not in frames and observed['frame_id'] == frame, 'FRAME_BINDING')
            frames.add(frame)
            require((observed['width'], observed['height']) == (stream['width'], stream['height'])
                    and observed['image_topic'] == ns + '/' + role + '/image_raw'
                    and observed['camera_info_topic'] == ns + '/' + role + '/camera_info'
                    and observed['metadata_topic'] == ns + '/' + role + '/frame_metadata', 'CONSUMER_PROFILE_BINDING')
        require(spec['pair']['topic'] == ns + '/rgbd/pair_metadata'
                and spec['pair']['clock'] == config['pairing']['clock']
                and spec['pair']['max_delta_us'] == config['pairing']['max_delta_us'], 'CONSUMER_PAIR_BINDING')
        rows.append(dict(camera=camera, inputs=inputs, config=config, spec=spec))
        public.append(dict(id=name, image=camera['image'], configuration_sha256=digest(inputs['capture_config']),
                           runtime_sha256=digest(inputs['runtime_manifest']), consumer_sha256=digest(inputs['consumer_spec']),
                           color={k: config['color'][k] for k in ('width', 'height', 'fps')},
                           depth={k: config['depth'][k] for k in ('width', 'height', 'fps')}))
    require(len(generations) == 1, 'GENERATION_MISMATCH')
    description = dict(version=1, kind='multicamera-test-plan', seconds=value['seconds'], cameras=public,
                       production_camera_limit_unchanged=3, usbfs_min_mb=value['usbfs_min_mb'], capacity_certified=False, execution_requested=False)
    description['plan_sha256'] = digest(canonical(description))
    return description, rows


def exclusive(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def snapshot_matches(status, config, spec, directory):
    identity = status.get('snapshot_id', '')
    require(isinstance(identity, str) and re.fullmatch('[0-9a-f]{64}', identity), 'SNAPSHOT_REFERENCE')
    snapshot = parse(private_read(directory / 'status' / 'snapshots' / (identity + '.json')))
    content, binding = snapshot['content'], snapshot['content']['binding']
    require(status.get('generation') == config['service']['generation'], 'RUNNING_GENERATION_MISMATCH')
    require(isinstance(status.get('session_id'), str) and re.fullmatch('[0-9a-f]{32}', status['session_id'])
            and isinstance(status.get('instance_id'), str) and re.fullmatch('[0-9a-f]{64}', status['instance_id']), 'RUNNING_IDENTITY_INVALID')
    require(snapshot['snapshot_id'] == identity and binding['session_id'] == status['session_id']
            and binding['instance_id'] == status['instance_id'] and binding['generation_id'] == status['generation']
            and content['configuration']['runtime_config_sha256'] == status['config_sha256'], 'SNAPSHOT_RUNTIME_BINDING')
    require(content['calibration']['source'] == 'sdk-active-profiles'
            and content['calibration']['physical_accuracy_verified'] is False, 'SNAPSHOT_PROVENANCE')
    for role in ('color', 'depth'):
        model, expected = content['calibration']['models'][role], spec['streams'][role]
        require(model['frame_id'] == expected['frame_id'] and model['width'] == expected['width']
                and model['height'] == expected['height'], 'CURRENT_PROFILE_CHANGED')
        k = model['k']
        distortion = model['distortion_model'].replace('-', '_')
        coefficients = model['distortion_coefficients']
        if distortion == 'none':
            distortion, coefficients = 'plumb_bob', [0.] * 5
        calibration = expected['calibration']
        require(calibration['distortion_model'] == distortion, 'CURRENT_CALIBRATION_CHANGED')
        for actual, wanted in ((k, calibration['k']), (coefficients, calibration['d']),
                               ([1., 0., 0., 0., 1., 0., 0., 0., 1.], calibration['r']),
                               ([k[0], k[1], k[2], 0., k[3], k[4], k[5], 0., k[6], k[7], k[8], 0.], calibration['p'])):
            require(len(actual) == len(wanted) and all(math.isclose(a, b, abs_tol=1e-6, rel_tol=0) for a, b in zip(actual, wanted)), 'CURRENT_CALIBRATION_CHANGED')
    transforms = [t for t in content['calibration']['transforms'] if t['source_frame'] == spec['tf']['child_frame']
                  and t['target_frame'] == spec['tf']['parent_frame']]
    require(len(transforms) == 1, 'CURRENT_TRANSFORM_BINDING')
    transform = transforms[0]
    actual = rotation_quaternion(transform['rotation_row_major'])
    expected = spec['tf']['rotation_xyzw']
    require(min(max(abs(a-b) for a,b in zip(actual, expected)), max(abs(a+b) for a,b in zip(actual, expected))) <= spec['tf']['tolerance']
            and all(abs(a-b) <= spec['tf']['tolerance'] for a,b in zip(transform['translation_m'], spec['tf']['translation_m'])), 'CURRENT_TRANSFORM_CHANGED')


class Docker:
    def run(self, args):
        return subprocess.check_output(['docker', *args], stderr=subprocess.PIPE, timeout=60)
    def inspect(self, name):
        return parse(self.run(['inspect', name]))[0]


def execute(description, rows, output, docker=None, guard_path=None):
    require(os.geteuid() == 0 or docker is not None, 'ROOTFUL_TEST_EXECUTION_REQUIRED')
    simulated = docker is not None
    docker = docker or Docker()
    if not simulated:
        require(os.environ.get('DOCKER_HOST', '') in ('', 'unix:///var/run/docker.sock'), 'LOCAL_DOCKER_REQUIRED')
        try:
            current = int(Path('/sys/module/usbcore/parameters/usbfs_memory_mb').read_text().strip())
        except (OSError, ValueError):
            raise TestError('USBFS_UNAVAILABLE') from None
        require(current == 0 or current >= description['usbfs_min_mb'], 'USBFS_RESOURCE_INSUFFICIENT')
    context = parse(docker.run(['context', 'inspect']))[0]
    require(context['Endpoints']['docker']['Host'] == 'unix:///var/run/docker.sock', 'LOCAL_DOCKER_REQUIRED')
    security = parse(docker.run(['info', '--format', '{{json .SecurityOptions}}']))
    require(isinstance(security, list) and all(isinstance(v, str) for v in security)
            and not any('rootless' in v for v in security), 'ROOTFUL_DOCKER_REQUIRED')
    output = Path(output)
    require(output.is_absolute() and not output.exists() and not output.is_symlink(), 'NEW_ABSOLUTE_OUTPUT_REQUIRED')
    # Reject symlink ancestors even for caller-selected output locations.
    parent_fd = open_no_follow(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    os.close(parent_fd)
    output.mkdir(mode=0o700)
    run_id = uuid.uuid4().hex
    created, active, errors = [], [], []
    summary = dict(version=1, simulated=simulated, run_id=run_id, complete=False, plan_sha256=description['plan_sha256'], cameras=[])
    try:
        exclusive(output / 'plan.json', canonical(description))
        for row in rows:
            require(guard_path is None or not os.path.lexists(guard_path), 'RECORDING_PROTECTED')
            camera, config = row['camera'], row['config']
            # Recheck caller-owned inputs before copying; a modified file voids the plan.
            require(all(private_read(camera[key]) == raw for key, raw in row['inputs'].items()), 'INPUT_CHANGED_AFTER_PLAN')
            image = docker.inspect(camera['image'])
            require(image['Config']['Labels'].get('org.geminidepthport.capture-backend') == 'rust-v1', 'CAPTURE_IMAGE_REQUIRED')
            image_id = image['Id']
            require(isinstance(image_id, str) and re.fullmatch('sha256:[0-9a-f]{64}', image_id), 'IMAGE_IDENTITY_INVALID')
            # Pin the resolved image for every subsequent operation.
            directory = output / camera['id']
            directory.mkdir(mode=0o700)
            for name in ('status', 'recording'):
                (directory / name).mkdir(mode=0o700)
            for key, name in (('capture_config', 'capture.json'), ('runtime_manifest', 'runtime.json'), ('consumer_spec', 'consumer.json')):
                exclusive(directory / name, row['inputs'][key])
            name = 'gdp-test-' + run_id[:12] + '-' + camera['id']
            base = ['--label', LABEL + '=' + run_id, '--network', 'host', '--ipc', 'private', '--read-only', '--cap-drop', 'ALL',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m', '-e', 'ROS_DOMAIN_ID=74', '--log-driver', 'local', '--log-opt', 'max-size=4m', '--log-opt', 'max-file=1', '--log-opt', 'compress=false']
            created.append(name)  # A daemon may create before the client raises/times out.
            docker.run(['run', '-d', '--name', name, *base, '--device', '/dev/bus/usb',
                        '-v', str(directory / 'capture.json') + ':/config/capture.json:ro',
                        '-v', str(directory / 'runtime.json') + ':/config/runtime.json:ro',
                        '-v', str(directory / 'status') + ':/tmp/capture',
                        '-v', str(directory / 'recording') + ':/recording', image_id])
            deadline = time.monotonic() + config['service']['startup_timeout_ms'] / 1000 + 5
            while True:
                require(guard_path is None or not os.path.lexists(guard_path), 'RECORDING_PROTECTED')
                running = docker.inspect(name)
                require(running['Config']['Labels'].get(LABEL) == run_id, 'CAPTURE_OWNER_MISMATCH')
                require(running['State']['Running'], 'CAPTURE_START_FAILED')
                try:
                    status = parse(private_read(directory / 'status/status.json'))
                    if status['state'] == 'ready':
                        require(status['config_sha256'] == digest(row['inputs']['capture_config']), 'RUNNING_CONFIG_MISMATCH')
                        snapshot_matches(status, config, row['spec'], directory)
                        break
                except FileNotFoundError:
                    pass
                require(time.monotonic() < deadline, 'CAPTURE_START_DEADLINE')
                time.sleep(.1)
            active.append(dict(row=row, directory=directory, name=name, image=image_id, baseline=status))
        source = ROOT.resolve()
        for item in active:
            name = item['name'] + '-consumer'
            created.append(name)
            docker.run(['run', '-d', '--name', name, '--label', LABEL + '=' + run_id, '--network', 'host', '--ipc', 'private',
                        '--read-only', '--cap-drop', 'ALL', '--tmpfs', '/tmp:rw,size=128m', '-e', 'ROS_DOMAIN_ID=74', '--log-driver', 'local', '--log-opt', 'max-size=4m', '--log-opt', 'max-file=1', '--log-opt', 'compress=false',
                        '-v', str(source) + ':/validation-source:ro', '-v', str(item['directory']) + ':/validation',
                        item['image'], 'python3', '/validation-source/runtime/rgbd_acceptance.py', '--spec', '/validation/consumer.json',
                        '--seconds', str(description['seconds']), '--summary', '/validation/consumer-result.json'])
            item['consumer'] = name
        started = time.monotonic()
        with (output / 'observations.jsonl').open('xb') as trace:
            os.chmod(output / 'observations.jsonl', 0o600)
            while True:
                require(guard_path is None or not os.path.lexists(guard_path), 'RECORDING_PROTECTED')
                elapsed = time.monotonic() - started
                for item in active:
                    status = parse(private_read(item['directory'] / 'status/status.json'))
                    old = item['baseline']
                    changed = any(status.get(key) != old.get(key) for key in ('session_id', 'instance_id', 'generation', 'config_sha256'))
                    if changed or status.get('state') != 'ready':
                        errors.append(dict(camera=item['row']['camera']['id'], elapsed_seconds=elapsed,
                                           code='SESSION_CHANGED' if changed else 'NOT_READY'))
                    line = canonical(dict(camera=item['row']['camera']['id'], elapsed_seconds=elapsed, status=status)) + b'\n'
                    require(trace.tell() + len(line) <= 64 * 1024 * 1024, 'OBSERVATION_OUTPUT_LIMIT')
                    trace.write(line)
                if elapsed >= description['seconds']:
                    break
                time.sleep(min(1, description['seconds'] - elapsed))
        capture_window_seconds = time.monotonic() - started
        # The consumer has its own discovery/window/drain. Do not stop publishers early.
        deadline = time.monotonic() + 25
        while any(docker.inspect(item['consumer'])['State']['Running'] for item in active):
            require(guard_path is None or not os.path.lexists(guard_path), 'RECORDING_PROTECTED')
            require(time.monotonic() < deadline, 'CONSUMER_FINISH_DEADLINE')
            time.sleep(.1)
        require(guard_path is None or not os.path.lexists(guard_path), 'RECORDING_PROTECTED')
        for item in active:
            consumer_info = docker.inspect(item['consumer'])
            require(consumer_info['Config']['Labels'].get(LABEL) == run_id, 'CONSUMER_OWNER_MISMATCH')
            consumer_state = consumer_info['State']
            consumer = parse(private_read(item['directory'] / 'consumer-result.json'))
            summary['cameras'].append(dict(id=item['row']['camera']['id'], consumer_ok=consumer.get('ok') is True and type(consumer_state.get('ExitCode')) is int and consumer_state['ExitCode'] == 0,
                                           matched_pairs=consumer.get('matched_pairs'), contract_errors=consumer.get('contract_errors', {})))
        summary.update(complete=True, capture_window_seconds=capture_window_seconds, elapsed_with_consumer_drain_seconds=time.monotonic() - started,
                       capture_observations_ok=not errors, observation_exceptions=errors,
                       all_consumers_passed=all(c['consumer_ok'] for c in summary['cameras']))
        return summary
    finally:
        cause = sys.exc_info()[1]
        cleanup_errors = []
        try:
            # Discover containers that were created even if `docker run` timed out.
            listed = docker.run(['ps', '-a', '--filter', 'label=' + LABEL + '=' + run_id, '--format', '{{.Names}}']).decode().splitlines()
            owned = set(listed)
            if cause is None and any(name not in owned for name in created):
                cleanup_errors.append('CREATED_CONTAINER_NOT_OWNED_OR_MISSING')
        except Exception:
            owned = set(created)
            cleanup_errors.append('OWNED_CONTAINER_DISCOVERY_FAILED')
        for name in reversed(created):
            if name not in owned:
                continue
            try:
                info = docker.inspect(name)
                require(info['Config']['Labels'].get(LABEL) == run_id, 'CLEANUP_OWNER_MISMATCH')
                if info['State']['Running']:
                    docker.run(['stop', '-t', '8', name])
                exclusive(output / (name + '.log'), docker.run(['logs', name]))
            except Exception:
                cleanup_errors.append(name)
        summary['cleanup_ok'] = not cleanup_errors
        if cleanup_errors:
            exclusive(output / 'CLEANUP_REQUIRED', ('\n'.join(cleanup_errors) + '\n').encode())
            summary['complete'] = False
        if cause:
            summary['complete'] = False
            summary['error'] = str(cause) if isinstance(cause, TestError) else 'TEST_RUNNER_FAILED'
        summary['execution_finished'] = summary['complete']
        summary['complete'] = bool(summary['complete'] and summary.get('all_consumers_passed') and summary.get('capture_observations_ok') and summary['cleanup_ok'])
        summary['ok'] = bool(summary['complete'] and summary.get('all_consumers_passed')
                             and summary.get('capture_observations_ok') and summary['cleanup_ok'])
        exclusive(output / 'result.json', canonical(summary))
        if summary['complete']:
            exclusive(output / 'COMPLETE', b'test execution completed; inspect result acceptance fields\n')
        if cleanup_errors and cause is None:
            raise TestError('CLEANUP_INCOMPLETE')


@contextmanager
def production_lock(root):
    """Use the same inode/protocol as Controller.lock; never bypass a guard."""
    root = Path(root)
    root_fd = open_no_follow(root, os.O_RDONLY | os.O_DIRECTORY)
    os.close(root_fd)
    state = root / 'state'
    info = state.lstat()
    require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700, 'PRIVATE_MANAGEMENT_STATE_REQUIRED')
    path = state / 'control.lock'
    try:
        fd = open_no_follow(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, info.st_uid, info.st_gid)
    except FileExistsError:
        fd = open_no_follow(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        lock_info = os.fstat(fd)
        require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_nlink == 1
                and lock_info.st_uid == info.st_uid and stat.S_IMODE(lock_info.st_mode) == 0o600, 'PRIVATE_CONTROL_LOCK_REQUIRED')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TestError('MANAGEMENT_OPERATION_IN_PROGRESS') from None
        require(not os.path.lexists(state / 'recording-guard.json'), 'RECORDING_PROTECTED')
        yield state
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-plan')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--management-root', type=Path)
    args = parser.parse_args()
    try:
        description, rows = plan(args.manifest)
        if args.execute:
            require(args.confirm_plan == description['plan_sha256'] and args.output is not None, 'CONFIRMED_PLAN_AND_OUTPUT_REQUIRED')
            require(args.management_root is not None, 'MANAGEMENT_ROOT_REQUIRED')
            with production_lock(args.management_root) as state:
                result = execute(description, rows, args.output, guard_path=state / 'recording-guard.json')
        else:
            require(args.confirm_plan is None and args.output is None and args.management_root is None, 'PLAN_ARGUMENTS')
            result = description
        print(json.dumps(result, allow_nan=False))
        return 0 if result.get('ok', True) else 1
    except Exception as error:
        print(json.dumps({'version': 1, 'ok': False, 'code': str(error) if isinstance(error, TestError) else 'TEST_RUNNER_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
