#!/usr/bin/env python3
"""Prepare, then explicitly execute the real Rust management acceptance flow.

Run on the authorized Linux target. Default mode only creates a new private,
unconfirmed inventory and calls public CLI draft validation. --execute additionally
uses actual preview/confirm HTTP APIs, generation/up, calibration export, recording
guard and down. It never writes a positive verification flag or fabricates a
calibration. Do not execute while a separate capture test owns the devices.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[2]
PROJECT = 'orbbec-multicam'
PARAMETERS = {'enable_color', 'enable_depth', 'color_format', 'depth_format', 'depth_registration',
              'color_width', 'color_height', 'color_fps', 'depth_width', 'depth_height', 'depth_fps',
              'color_qos', 'color_camera_info_qos', 'depth_qos', 'depth_camera_info_qos'}


class AcceptanceFailure(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise AcceptanceFailure(code)


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.write('\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_output(text):
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict): return value
        except ValueError:
            pass
    raise AcceptanceFailure('PUBLIC_COMMAND_JSON_MISSING')


def candidate(source, image, policy):
    require(source.get('schema_version') == 2 and isinstance(source.get('cameras'), list), 'SOURCE_SCHEMA')
    result = copy.deepcopy(source)
    result.update(capture_backend='rust', image=image, rust_capture=copy.deepcopy(policy))
    active = 0
    for camera in result['cameras']:
        require(isinstance(camera, dict) and isinstance(camera.get('parameters'), dict), 'SOURCE_CAMERA')
        require(set(camera['parameters']) <= PARAMETERS, 'SOURCE_UNSUPPORTED_PARAMETERS')
        # Removing a stale proof is allowed; only the real confirmation API below
        # may restore profiles_verified and its runtime-bound evidence.
        camera.update(profiles_verified=False, verification=None)
        if camera.get('enabled') is True:
            active += 1
            require(bool(camera.get('serial_number')), 'SOURCE_BINDING_MISSING')
            require(camera['parameters'].get('enable_color') is True and camera['parameters'].get('enable_depth') is True, 'SOURCE_RGBD_REQUIRED')
            camera['timestamp_source'] = 'host'  # Explicit Rust SDK-system Unix mapping.
    require(1 <= active <= 3, 'SOURCE_ACTIVE_CAMERA_COUNT')
    return result


def context_bindings(value, names, protected):
    """Check facts returned by the real manager; never construct positive proof."""
    require(value.get('version') == 1 and value.get('kind') == 'recording-context'
            and value.get('backend') == 'rust' and value.get('physical_accuracy_verified') is False,
            'CONTEXT_SCHEMA')
    protection = value.get('protection', {})
    require(protection.get('guard_version') == 2
            and protection.get('strong_guarantee_available') is True
            and protection.get('native_restart_gate') is True
            and protection.get('session_bound') is protected
            and protection.get('native_gate_ack_verified') is protected,
            'CONTEXT_NATIVE_PROTECTION')
    if protected:
        require(protection.get('state') == 'active', 'CONTEXT_GUARD_NOT_ACTIVE')
    cameras = value.get('cameras', [])
    require(len(cameras) == len(names) and {c.get('camera_name') for c in cameras} == set(names),
            'CONTEXT_CAMERA_SET')
    bindings = {}
    for camera in cameras:
        snapshot = camera.get('snapshot', {})
        content = snapshot.get('content', {})
        binding = content.get('binding', {})
        calibration = content.get('calibration', {})
        require(camera.get('session_kind') == 'sdk-worker'
                and snapshot.get('version') == 1
                and re.fullmatch('[a-f0-9]{64}', snapshot.get('snapshot_id', '')) is not None
                and binding.get('instance_id') == camera.get('instance_id')
                and binding.get('session_id') == camera.get('session_id')
                and binding.get('generation_id') == value.get('generation_id')
                and calibration.get('source') == 'sdk-active-profiles'
                and calibration.get('actual_runtime_observed') is True
                and calibration.get('physical_accuracy_verified') is False
                and calibration.get('source_session_id') == camera.get('session_id')
                and calibration.get('source_instance_id') == camera.get('instance_id')
                and re.fullmatch('[a-f0-9]{64}', content.get('configuration', {}).get('runtime_config_sha256', '')) is not None,
                'CONTEXT_SNAPSHOT_BINDING')
        bindings[camera['camera_name']] = {key: camera.get(key) for key in
            ('container_id', 'instance_id', 'session_id', 'snapshot')}
    return bindings


def quality_binding(value, name, binding, generation, kind):
    require(value.get('version') == 1 and value.get('kind') == 'quality-' + kind
            and value.get('backend') == 'rust' and value.get('camera_name') == name
            and value.get('session_kind') == 'sdk-worker' and value.get('worker_ready') is True
            and value.get('instance_id') == binding['instance_id']
            and value.get('session_id') == binding['session_id']
            and value.get('snapshot_id') == binding['snapshot']['snapshot_id']
            and value.get('generation_id') == generation, 'QUALITY_RUNTIME_BINDING')
    return value.get('result', {})


class Runner:
    def __init__(self, args):
        self.args = args
        self.workspace = args.workspace
        self.logs = self.workspace / 'acceptance-logs'
        self.process = None
        self.drain_thread = None
        self.log_handle = None
        self.base = self.token = self.guard_token = None
        self.claimed_project = False
        self.completed = []
        self.cleanup_ok = True
        self.owns_workspace = False
        self.preview_names = set()

    def cli(self, stage, *arguments, expected_error=None, credential=None, timeout=1200):
        result = subprocess.run([str(self.args.manager_binary), '--root', str(self.workspace), *arguments],
                                input=credential, text=True, capture_output=True, timeout=timeout)
        # Guard credentials remain in memory/stdin only, never private logs either.
        if credential is None and (not arguments or arguments[0] != 'protect'):
            (self.logs / (stage + '.stdout')).write_text(result.stdout)
            (self.logs / (stage + '.stderr')).write_text(result.stderr)
        if expected_error:
            require(result.returncode != 0 and expected_error in result.stderr, 'EXPECTED_GUARD_REJECTION_MISSING')
            return None
        require(result.returncode == 0, 'PUBLIC_COMMAND_FAILED_' + stage.upper())
        return json_output(result.stdout) if result.stdout.strip() else {}

    def http(self, path, payload=None, timeout=60, expected_error=None):
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers = {'Content-Type': 'application/json', 'Origin': self.base, 'X-CSRF-Token': self.token}
        request = Request(self.base + path, data=body, headers=headers)
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                raw = response.read()
                require(expected_error is None, 'EXPECTED_HTTP_GUARD_REJECTION_MISSING')
                return raw if path == '/' else json.loads(raw)
        except HTTPError as error:
            value = json.loads(error.read())
            if self.owns_workspace:
                private_json(self.logs / ('http-' + path.strip('/').replace('/', '-') + '-private.json'), value)
            require(expected_error is not None and error.code == 400 and expected_error in value.get('error', ''), 'PUBLIC_HTTP_FAILED')
            return value

    def prepare(self):
        source = json.loads(self.args.source_inventory.read_text())
        policy = json.loads(self.args.policy_file.read_text()) if self.args.policy_file else source.get('rust_capture')
        require(isinstance(policy, dict), 'EXPLICIT_RUST_POLICY_REQUIRED')
        data = candidate(source, self.args.image, policy)
        require(not self.workspace.exists(), 'WORKSPACE_EXISTS_USE_NEW_PRIVATE_DIRECTORY')
        self.workspace.mkdir(mode=0o700, parents=False)
        self.owns_workspace = True
        self.logs.mkdir(mode=0o700)
        shutil.copytree(REPO / 'web', self.workspace / 'web')
        (self.workspace / 'patches/public-orbbec').mkdir(mode=0o700, parents=True)
        shutil.copyfile(REPO / 'patches/public-orbbec/manifest.json', self.workspace / 'patches/public-orbbec/manifest.json')
        private_json(self.workspace / 'inventory.json', data)
        private_json(self.workspace / 'preparation.json', {'version': 1, 'source_inventory_sha256': digest(self.args.source_inventory),
            'candidate_sha256': digest(self.workspace / 'inventory.json'), 'manager_binary_sha256': digest(self.args.manager_binary),
            'positive_confirmation_written': False, 'timestamp_mapping_explicitly_redeclared': 'SDK system Unix via host',
            'source_parameters_dropped': False, 'execute_requested': self.args.execute})
        self.cli('draft-validate', 'validate', '--draft')
        self.completed.append('unconfirmed-draft-validation')
        return data

    def start_manager(self):
        self.log_handle = open(self.logs / 'manager.log', 'x')
        self.process = subprocess.Popen([str(self.args.manager_binary), '--root', str(self.workspace), 'serve', '--port', '0'],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if selector.select(timeout=.2):
                    line = self.process.stdout.readline()
                    self.log_handle.write(line); self.log_handle.flush()
                    match = re.search(r'http://127\.0\.0\.1:(\d+)', line)
                    if match:
                        self.base = match.group(0)
                        break
                require(self.process.poll() is None, 'MANAGEMENT_SERVER_EXITED')
            require(self.base is not None, 'MANAGEMENT_SERVER_START_TIMEOUT')
        finally:
            selector.close()
        def drain():
            for line in self.process.stdout:
                self.log_handle.write(line); self.log_handle.flush()
        self.drain_thread = threading.Thread(target=drain, daemon=True)
        self.drain_thread.start()
        token = re.search(rb'name="csrf-token" content="([^"]+)"', self.http('/'))
        require(token is not None, 'PUBLIC_CSRF_TOKEN_MISSING')
        self.token = token.group(1).decode()

    def verify_context(self, names, protected, previous=None):
        value = self.cli('context-' + ('protected' if protected else 'unprotected'),
                         'recording-context', '--require-strong')
        bindings = context_bindings(value, names, protected)
        require(previous is None or bindings == previous, 'CONTEXT_RUNTIME_CHANGED')
        return value, bindings

    def verify_quality(self, context, bindings):
        for name, binding in bindings.items():
            query = urlencode({'camera': name})
            current = quality_binding(self.http('/api/quality/current?' + query), name, binding,
                                      context['generation_id'], 'current')['quality']
            require(current.get('session_id') == binding['session_id']
                    and current.get('current_session_finalized') is False, 'QUALITY_CURRENT_INVALID')
            page = quality_binding(self.http('/api/quality/events?' + query + '&limit=2'), name,
                                   binding, context['generation_id'], 'events')['page']
            require(page.get('origin_session_id') == current.get('origin_session_id')
                    and isinstance(page.get('events'), list)
                    and page.get('latest_sequence', -1) >= current.get('latest_event_sequence', 0),
                    'QUALITY_EVENTS_INVALID')
            cursor = page.get('next_cursor')
            require(isinstance(cursor, dict), 'QUALITY_CURSOR_MISSING')
            next_page = quality_binding(self.http('/api/quality/events?' + urlencode(
                {'camera': name, 'limit': 2, 'cursor': json.dumps(cursor)})), name, binding,
                context['generation_id'], 'events')['page']
            require(next_page.get('origin_session_id') == cursor.get('origin_session_id')
                    and all(event.get('sequence', -1) > cursor.get('after_sequence', -1)
                            for event in next_page.get('events', [])), 'QUALITY_CURSOR_NOT_APPLIED')
            summary = quality_binding(self.http('/api/quality/session-summary?' + query), name,
                                      binding, context['generation_id'], 'session-summary')
            # An active session must not manufacture a finalized summary.
            require(summary.get('available') is False and summary.get('summary') is None
                    and isinstance(summary.get('finalized_sessions'), list), 'QUALITY_ACTIVE_SUMMARY_INVALID')
        self.completed.append('real-quality-current-events-summary-apis')

    def execute(self, data):
        require(os.name == 'posix' and Path('/proc').is_dir() and self.args.devices_released, 'EXPLICIT_TARGET_AND_DEVICE_RELEASE_REQUIRED')
        for root in (self.args.source_deployment_root or self.args.source_inventory.parent, self.workspace):
            guard = root / 'state/recording-guard.json'
            require(not guard.exists() and not guard.is_symlink(), 'EXISTING_RECORDING_PROTECTION')
        # The manager uses a fixed Compose project. Refuse preexisting instances
        # rather than stop, adopt, or overwrite another deployment's resources.
        found = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT], text=True, capture_output=True, check=True)
        previews = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=orbbec.web.preview=' + PROJECT], text=True, capture_output=True, check=True)
        require(not found.stdout.strip() and not previews.stdout.strip(), 'MANAGED_PROJECT_ALREADY_EXISTS')
        self.start_manager()
        self.claimed_project = True
        for camera in data['cameras']:
            if camera.get('enabled') is not True: continue
            name = camera['name']
            self.preview_names.add(name)
            self.http('/api/preview/start', {'role': name}, timeout=120)
            deadline = time.monotonic() + self.args.preview_timeout_seconds
            while time.monotonic() < deadline:
                state = self.http('/api/state')  # Renews the actual preview lease.
                preview = state['previews'].get(name, {})
                streams = preview.get('streams', {})
                if preview.get('running') and all(streams.get(stream, {}).get('frames', 0) >= 3
                        and 0 <= time.time() - streams[stream].get('received_at', 0) <= 3 for stream in ('color', 'depth')):
                    break
                time.sleep(.5)
            else:
                raise AcceptanceFailure('PREVIEW_REAL_FRAMES_TIMEOUT')
            self.http('/api/confirm', {'role': name}, timeout=120)
            # This is evidence returned by the real API, not a direct file edit.
            verified = self.http('/api/state')['inventory']
            current = next(c for c in verified['cameras'] if c['name'] == name)
            require(current.get('profiles_verified') is True and current.get('verification', {}).get('method') == 'runtime-probe'
                    and re.fullmatch('[a-f0-9]{64}', current['verification'].get('runtime_digest', '')), 'REAL_CONFIRMATION_NOT_RECORDED')
            self.save_preview_logs(name)
            self.http('/api/preview/stop', {'role': name})
        self.completed.append('actual-preview-confirmation')
        self.cli('confirmed-validate', 'validate')
        self.cli('render', 'render')
        self.completed.append('runtime-bound-generation')
        require(self.cli('up', 'up').get('ready') is True, 'FORMAL_GROUP_NOT_READY')
        require(self.cli('check', 'check').get('ready') is True, 'FORMAL_CONSUMPTION_FAILED')
        self.completed.append('formal-up-and-real-health')
        calibration = self.cli('calibration-export', 'calibration-export')
        require(calibration.get('structural_validation') is True and calibration.get('physical_accuracy_verified') is False, 'CALIBRATION_EXPORT_FAILED')
        self.completed.append('published-calibration-export')
        names = [c['name'] for c in data['cameras'] if c.get('enabled') is True]
        context, bindings = self.verify_context(names, False)
        self.verify_quality(context, bindings)
        protected = self.cli('protect', 'protect', '--require-strong')
        self.guard_token = protected.get('token')
        require(isinstance(self.guard_token, str) and self.guard_token, 'GUARD_CREDENTIAL_MISSING')
        require(protected.get('runtime_session_bound') is True and protected.get('state') == 'active',
                'NATIVE_GUARD_NOT_BOUND')
        self.verify_context(names, True, bindings)
        self.completed.append('native-gate-ack-and-snapshot-bound-context')
        self.cli('guard-blocks-down', 'down', expected_error='RECORDING_PROTECTED')
        first = next(c['name'] for c in data['cameras'] if c.get('enabled') is True)
        self.http('/api/preview/start', {'role': first}, expected_error='RECORDING_PROTECTED')
        self.cli('wrong-release', 'release-protection', credential='not-the-credential', expected_error='GUARD_CREDENTIAL')
        self.verify_context(names, True, bindings)  # Wrong credential must preserve ACK/binding.
        require(self.cli('guard-preserves-health', 'check').get('ready') is True, 'GUARD_INTERRUPTED_CAPTURE')
        self.cli('release', 'release-protection', credential=self.guard_token)
        self.guard_token = None
        self.verify_context(names, False, bindings)
        self.completed.append('recording-guard-interlock')
        require(self.cli('down', 'down').get('status') == 'stopped', 'FORMAL_STOP_FAILED')
        self.completed.append('formal-down')
        for name,binding in bindings.items():
            history=self.cli('historical-summary-'+name,'quality-session-summary',name,
                '--generation',context['generation_id'],'--session',binding['session_id'])
            require(history.get('live') is False and history.get('worker_ready') is False
                and history.get('native_gate_ack_verified') is False and history.get('session_bound') is False
                and history.get('complete') is True and history.get('state')=='finalized'
                and history.get('snapshot_verified') is True
                and history.get('snapshot_id')==binding['snapshot']['snapshot_id'],
                'STOPPED_SESSION_SUMMARY_NOT_PERSISTED')
        self.completed.append('normal-down-persistent-session-summary')
        self.cli('remove-test-instances', 'stack-stop', '--remove')
        self.claimed_project = False

    def save_preview_logs(self, name):
        """Retain only this workspace's preview evidence before it is removed."""
        try:
            container = 'orbbec-web-preview-' + name
            inspected = subprocess.run(['docker', 'inspect', container], text=True,
                                       capture_output=True, timeout=15)
            if inspected.returncode != 0: return
            value = json.loads(inspected.stdout)[0]
            expected = str(self.workspace / 'work/web-preview' / name / 'config')
            if (value['Config']['Labels'].get('orbbec.web.preview') != PROJECT
                    or not any(m.get('Source') == expected and m.get('Destination') == '/config'
                               for m in value.get('Mounts', []))):
                return
            result = subprocess.run(['docker', 'logs', container], text=True,
                                    capture_output=True, timeout=15)
            (self.logs / ('preview-' + name + '-private.log')).write_text(result.stdout + result.stderr)
        except Exception:
            # Evidence collection must not prevent device-release cleanup.
            pass

    def cleanup(self):
        if self.claimed_project:
            for name in self.preview_names:
                self.save_preview_logs(name)
        if self.guard_token:
            try:
                self.cli('cleanup-release', 'release-protection', credential=self.guard_token)
                self.guard_token = None
            except Exception:
                self.cleanup_ok = False  # Never delete a protection record directly.
        if self.claimed_project and not self.guard_token:
            try: self.cli('cleanup-stop', 'stack-stop', '--remove')
            except Exception: self.cleanup_ok = False
        if self.process:
            self.process.terminate()
            try: self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(); self.cleanup_ok = False
            if self.drain_thread: self.drain_thread.join(timeout=2)
            self.process.stdout.close()
        if self.log_handle: self.log_handle.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-inventory', type=Path, required=True)
    parser.add_argument('--source-deployment-root', type=Path)
    parser.add_argument('--workspace', type=Path, required=True, help='New private directory; must not exist')
    parser.add_argument('--manager-binary', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--policy-file', type=Path, help='Explicit rust_capture policy if not present in source inventory')
    parser.add_argument('--execute', action='store_true', help='Actually operate only the selected test cameras through public manager APIs')
    parser.add_argument('--devices-released', action='store_true', help='Explicitly assert other SDK/driver tests have released these devices')
    parser.add_argument('--preview-timeout-seconds', type=int, default=120)
    args = parser.parse_args(argv)
    os.umask(0o077)
    runner = Runner(args)
    report = {'version': 1, 'kind': 'formal-rust-management-acceptance', 'ok': False,
              'executed': args.execute, 'hardware_verified': False, 'physical_accuracy_verified': False}
    try:
        require(all(p.is_absolute() for p in (args.source_inventory, args.workspace, args.manager_binary))
                and args.source_inventory.is_file() and args.manager_binary.is_file()
                and 10 <= args.preview_timeout_seconds <= 600, 'ARGUMENTS')
        require(args.source_deployment_root is None or args.source_deployment_root.is_absolute(), 'ARGUMENTS')
        data = runner.prepare()
        if args.execute: runner.execute(data)
        report.update(ok=True, prepared=True, camera_count=sum(c.get('enabled') is True for c in data['cameras']),
                      actual_management_flow_verified=args.execute)
    except Exception as error:
        report['errors'] = [str(error) if isinstance(error, AcceptanceFailure) else 'MANAGEMENT_ACCEPTANCE_FAILED']
    finally:
        runner.cleanup()
        report.update(completed_stages=runner.completed, cleanup_ok=runner.cleanup_ok)
        report['ok'] = report['ok'] and runner.cleanup_ok
        if runner.owns_workspace:
            try: private_json(runner.workspace / 'acceptance-summary.json', report)
            except Exception: report['ok'] = False
    print(json.dumps(report, sort_keys=True))  # No identities, calibration, tokens or raw logs.
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
