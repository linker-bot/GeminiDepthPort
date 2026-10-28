#!/usr/bin/env python3
"""Loopback-only camera configuration server. Runs on the Linux Docker host."""
import argparse
import copy
import hmac
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import yaml
import cameractl as ctl

ROOT = Path(__file__).resolve().parent
ROLES = ('head', 'wrist')
EDITABLE = {'enable_color', 'color_width', 'color_height', 'color_fps', 'color_format',
            'enable_depth', 'depth_width', 'depth_height', 'depth_fps', 'depth_format',
            'enable_color_auto_exposure', 'color_exposure', 'color_gain',
            'enable_ir_auto_exposure', 'ir_exposure', 'ir_gain'}

def identify(product):
    normalized = re.sub('[^a-z0-9]', '', product.lower())
    for name in ('Gemini 335L', 'Gemini 2'):
        key = re.sub('[^a-z0-9]', '', name.lower())
        if normalized in (key, 'orbbec' + key):
            return name
    return None

def execute(args, timeout=15):
    result = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise ValueError(result.stdout.strip()[-3000:] or f'Command failed: {args[0]}')
    return result.stdout

class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.job = None
        self.active_job_id = None
        try:
            self.job = json.loads((ROOT/'state/latest-job.json').read_text())
            if self.job['status'] == 'running':
                self.job['status'] = 'failed'
                self.job['log'].append('Web server restarted before completion; inspect actual service state.')
        except (OSError, ValueError, KeyError): self.job = None
        self.preview_root = ROOT / 'work' / 'web-preview'
        self.last_seen = time.monotonic()
        self.closed = threading.Event()
        self.preview_names = {r: 'orbbec-web-preview-' + r for r in self.roles(ctl.load())}

    def devices(self):
        return [dict(d, model=identify(d['product'])) for d in ctl.scan()]

    def formal_running(self):
        return bool(execute(['docker', 'ps', '-q', '--filter', f'label=com.docker.compose.project={ctl.PROJECT}', '--filter', 'label=orbbec.role=camera']).strip())

    def managed(self, role):
        try:
            result = subprocess.run(['docker', 'inspect', self.preview_names[role]], cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
        except FileNotFoundError: return None
        if result.returncode:
            return None
        item = json.loads(result.stdout)[0]
        if (item['Config'].get('Labels') or {}).get('orbbec.web.preview') != ctl.PROJECT:
            raise ValueError('Container name belongs to another workload; refusing to operate')
        return item

    @ctl.mutation
    def stop_preview(self, role):
        item = self.managed(role)
        if item:
            if item['State']['Running']:
                execute(['docker', 'stop', '--time', '12', self.preview_names[role]], 25)
            execute(['docker', 'rm', self.preview_names[role]], 10)

    def stop_previews(self):
        return ctl.stop_previews()

    @staticmethod
    def roles(data):
        return tuple(c['name'] for c in data['cameras'])

    def role(self, data, role):
        if role not in self.roles(data):
            raise ValueError('Unknown camera role')
        return next(c for c in data['cameras'] if c['name'] == role)

    @staticmethod
    def resolved(data, camera):
        if data['schema_version'] == 1:
            return dict(camera, namespace='/' + camera['name'] + '_camera', frame_prefix=camera['name'] + '_camera')
        return dict(camera, model=ctl.config.PROFILE_CATALOG['profiles'][camera['profile']]['model'],
                    launch_file=ctl.config.PROFILE_CATALOG['profiles'][camera['profile']]['launch_file'])

    @staticmethod
    def preview_signature(camera):
        content = {key: value for key, value in camera.items() if key not in ('profiles_verified', 'verification')}
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    @ctl.mutation
    def save_config(self, payload):
        data = copy.deepcopy(ctl.load())
        if data['schema_version'] == 2:
            return self.save_v2(data, payload)
        if payload.get('revision') != ctl.digest(ctl.INV):
            raise ValueError('Configuration changed in another window; reload before saving')
        updates = payload.get('cameras')
        if not isinstance(updates, list) or len(updates) != 2 or {c.get('name') for c in updates} != set(ROLES):
            raise ValueError('Both camera roles are required')
        devices = {d['serial_number']: d for d in self.devices() if d['serial_number']}
        changed_roles = []
        for update in updates:
            camera = self.role(data, update['name'])
            params = update.get('parameters', {})
            if not isinstance(params, dict) or set(params) - EDITABLE:
                raise ValueError('Unsupported editable parameter')
            serial = update.get('serial_number', camera['serial_number'])
            if not isinstance(serial, str) or not serial:
                raise ValueError('Invalid serial number')
            changed = serial != camera['serial_number'] or any(camera['parameters'].get(k) != v for k, v in params.items())
            if serial != camera['serial_number']:
                device = devices.get(serial)
                if not device or device['model'] != camera['model']:
                    raise ValueError(f"{camera['name']}: detected model must be {camera['model']}")
            if changed:
                changed_roles.append(camera['name'])
                camera['profiles_verified'] = False
            camera['serial_number'] = serial
            camera['parameters'].update(params)
            p = camera['parameters']
            for stream in ('color', 'depth'):
                for key in ('width', 'height', 'fps'):
                    value = p[stream + '_' + key]
                    maximum = 120 if key == 'fps' else 4096
                    if type(value) is not int or not 1 <= value <= maximum:
                        raise ValueError('Invalid image size or frame rate')
                if p[stream + '_format'] not in ('MJPG', 'RGB', 'YUYV', 'Y16', 'ANY'):
                    raise ValueError('Unsupported image format')
            for key in ('enable_color_auto_exposure', 'enable_ir_auto_exposure'):
                if key in p and type(p[key]) is not bool:
                    raise ValueError('Exposure switches must be booleans')
            for key in ('color_exposure', 'color_gain', 'ir_exposure', 'ir_gain'):
                if key in p and (type(p[key]) is not int or not -1 <= p[key] <= 1000000):
                    raise ValueError('Invalid exposure/gain')
        ctl.validate(data, draft=True)
        for role in changed_roles:
            if self.managed(role): self.stop_preview(role)
        ctl.save(data)
        return {'message': '配置已保存；修改过的设备需要重新预览并确认档位。'}

    def save_v2(self, data, payload):
        if payload.get('revision') != ctl.digest(ctl.INV):
            raise ValueError('Configuration changed; reload before saving')
        updates = payload.get('cameras')
        if (not isinstance(updates, list) or len(updates) != len(data['cameras'])
                or any(not isinstance(c, dict) for c in updates)
                or {c.get('name') for c in updates} != set(self.roles(data))):
            raise ValueError('All configured cameras are required')
        allowed = {'name', 'serial_number', 'enabled', 'namespace', 'frame_prefix',
                   'timestamp_source', 'calibration_revision', 'health', 'parameters'}
        devices = {d['serial_number']: d for d in self.devices() if d['serial_number']}
        changed = []
        for update in updates:
            if set(update) - allowed:
                raise ValueError('Unsupported editable field')
            camera = self.role(data, update['name'])
            before = self.preview_signature(camera)
            params = update.get('parameters', {})
            if not isinstance(params, dict) or set(params) - EDITABLE:
                raise ValueError('Unsupported editable parameter')
            serial = update.get('serial_number', camera['serial_number'])
            if serial and serial != camera['serial_number']:
                device = devices.get(serial)
                if not device or device['model'] != self.resolved(data, camera)['model']:
                    raise ValueError('Detected model does not match configured profile')
            camera.update({k: v for k, v in update.items() if k not in ('name', 'parameters')})
            camera['parameters'].update(params)
            if self.preview_signature(camera) != before:
                camera.update(profiles_verified=False, verification=None)
                changed.append(camera['name'])
        ctl.validate(data, draft=True)
        for role in changed:
            if self.managed(role): self.stop_preview(role)
        ctl.save(data)
        return {'message': '配置已保存；启用设备需完成实际档位检查，标定与语义验收另行执行。'}

    @ctl.mutation
    def start_preview(self, role):
        data = ctl.load(); ctl.validate(data, draft=True)
        camera = self.resolved(data, self.role(data, role))
        if data['schema_version'] == 2 and not camera['enabled']:
            raise ValueError('Enable and bind the camera before preview')
        if self.formal_running():
            raise ValueError('先停止正式服务，再启动调试预览。')
        found = [d for d in self.devices() if d['serial_number'] == camera['serial_number']]
        if len(found) != 1 or found[0]['model'] != camera['model']:
            raise ValueError('绑定设备未连接或型号不匹配，请先扫描并绑定。')
        if data['schema_version'] == 2:
            image = json.loads(ctl.run(['docker', 'image', 'inspect', data['image']], True))[0]
            labels = image.get('Config', {}).get('Labels') or {}
            ctl.config.require(labels.get('org.geminidepthport.native-yuyv') == '1'
                and labels.get('org.geminidepthport.explicit-bindings') == '1'
                and labels.get('org.geminidepthport.native-yuyv-patch') == ctl.driver_build.patch_identity()['patch_sha256']
                and labels.get('org.opencontainers.image.revision') == data['upstream_commit'],
                'IMAGE_CONTRACT_UNSUPPORTED', 'Preview requires the contracted driver image')
        self.stop_preview(role)
        folder = self.preview_root / role
        config = folder / 'config'; output = folder / 'out'
        config.mkdir(parents=True, exist_ok=True, mode=0o700); output.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700); config.chmod(0o700); output.chmod(0o700)
        for file in output.glob('*'):
            if file.is_file(): file.unlink()
        params = dict(camera['parameters'], camera_name=camera['frame_prefix'],
                      serial_number=camera['serial_number'], device_num=len(ctl.active(data)))
        if data['schema_version'] == 2:
            from runtime.qos_contract import effective_driver_qos
            params.update(effective_driver_qos(params))
            (config / 'binding.json').write_text(json.dumps({
                'schema_version': 2, 'namespace': camera['namespace'], 'frame_prefix': camera['frame_prefix'],
                'model': camera['model'], 'launch_file': camera['launch_file']}))
        (config / 'camera.yaml').write_text(yaml.safe_dump(params, sort_keys=False))
        streams = []
        for stream in ('color', 'depth'):
            if not params['enable_' + stream]: continue
            spec = {'topic': camera['namespace'] + '/' + stream + '/image_raw',
                    'width': params[stream+'_width'], 'height': params[stream+'_height'],
                    'min_fps': params[stream+'_fps'] * camera['health']['min_fps_ratio']}
            if data['schema_version'] == 2:
                spec.update(encoding='yuyv' if stream == 'color' else '16UC1',
                            frame_id=camera['frame_prefix'] + '_' + stream + '_optical_frame',
                            required_reliability='reliable',
                            camera_info_topic=camera['namespace'] + '/' + stream + '/camera_info')
            streams.append(spec)
        (config / 'health.json').write_text(json.dumps(dict(camera['health'], streams=streams,
                                                          camera_name=camera['frame_prefix'])))
        (config / 'revision').write_text(self.preview_signature(self.role(data, role)))
        (config / 'lease').touch()
        for private_file in config.iterdir():
            if private_file.is_file(): private_file.chmod(0o600)
        args = ['docker', 'run', '-d', '--init', '--name', self.preview_names[role],
                '--label', 'orbbec.web.preview=' + ctl.PROJECT, '--network', 'host', '--ipc', 'private',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--read-only',
                '--tmpfs', '/tmp:size=256m', '--tmpfs', '/root/.ros:size=128m',
                '--device-cgroup-rule', 'c 189:* rwm',
                '-v', '/dev/bus/usb:/dev/bus/usb:rw', '-v', '/run/udev:/run/udev:ro',
                '-v', f'{config}:/config:ro', '-v', f'{output}:/out:rw',
                '-e', 'ROS_DOMAIN_ID=' + str(data['ros_domain_id']), '-e', 'ROS_LOCALHOST_ONLY=1',
                '-e', 'RMW_IMPLEMENTATION=rmw_fastrtps_cpp',
                '-e', 'FASTRTPS_DEFAULT_PROFILES_FILE=/app/fastdds.xml',
                '-e', 'CAMERA_SCHEMA=' + str(data['schema_version']),
                '-e', 'CAMERA_NAMESPACE=' + camera['namespace'],
                '-e', 'CAMERA_ROLE=' + role, '-e', 'CAMERA_NAME=' + camera['frame_prefix'],
                '-e', 'CAMERA_MODEL=' + camera['model'], '-e', 'CAMERA_LAUNCH=' + camera['launch_file'],
                data['image'], 'python3', '/app/preview_session.py']
        execute(args, 30)
        return {'message': '预览容器已启动，等待实际图像。'}

    @ctl.mutation
    def confirm(self, role):
        data = ctl.load(); ctl.validate(data, draft=True); camera = self.role(data, role)
        folder = self.preview_root / role
        if (folder/'config/revision').read_text() != self.preview_signature(camera):
            raise ValueError('Preview config differs from saved config')
        item = self.managed(role)
        if not item or not item['State']['Running']:
            raise ValueError('Start a live preview before confirming')
        status = json.loads((folder/'out/status.json').read_text())
        for stream in ('color', 'depth'):
            if camera['parameters']['enable_'+stream]:
                st = status.get(stream, {})
                if time.time() - st.get('received_at', 0) > 3 or st.get('frames', 0) < 3:
                    raise ValueError('Both enabled streams must have fresh frames')
                if (st.get('width'), st.get('height')) != (camera['parameters'][stream+'_width'], camera['parameters'][stream+'_height']):
                    raise ValueError('Actual output dimensions differ from requested profile')
        # Active health test verifies timestamps, frame rate and freshness, not just preview images.
        execute(['docker', 'exec', self.preview_names[role], '/app/entrypoint.sh', 'python3', '/app/health.py'], 30)
        camera['profiles_verified'] = True
        if data['schema_version'] == 2:
            camera['verification'] = {'method': 'runtime-probe',
                                      'configuration_digest': ctl.config.camera_digest(data, camera)}
        ctl.save(data, _probe_confirmed=True)
        return {'message': '实际图像检查通过，档位已确认。'}

    def persist_job(self, job):
        folder = ROOT/'state/jobs'; folder.mkdir(parents=True, exist_ok=True)
        content = json.dumps(job, ensure_ascii=False, indent=2) + '\n'
        (folder/(job['id']+'.json')).write_text(content)
        latest = ROOT/'state/latest-job.json'; temp = latest.with_suffix('.tmp')
        temp.write_text(content); temp.replace(latest)
        for file in sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[50:]: file.unlink()

    def start_job(self, action):
        with ctl.control_lock(): ctl.guard.assert_unprotected(ctl.ROOT)
        if action not in ('build', 'render', 'preflight', 'up', 'check', 'down'):
            raise ValueError('Unknown action')
        if not self.lock.acquire(blocking=False):
            raise ValueError('另一个操作正在运行。')
        self.job = {'id': secrets.token_hex(6), 'action': action, 'status': 'running', 'log': [], 'started_at': time.time()}
        job = self.job
        self.active_job_id = job['id']
        job['inventory_revision'] = ctl.digest(ctl.INV)
        try: self.persist_job(job)
        except Exception:
            self.active_job_id = None
            self.lock.release(); raise
        def task():
            try:
                # Render strict configs before up; both profiles must have passed real preview checks.
                commands = [[action]]
                for command in commands:
                    script = 'deployment_control.py' if action == 'up' else 'cameractl.py'
                    process = subprocess.Popen([sys.executable, '-u', str(ROOT/script), *command],
                                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, start_new_session=True)
                    def timeout_process():
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    timer = threading.Timer(3600 if action == 'build' else 2400 if action == 'up' else 600, timeout_process)
                    timer.daemon = True; timer.start()
                    try:
                        for line in process.stdout:
                            job['log'].append(line.rstrip())
                            if len(job['log']) > 1200: del job['log'][:-1200]
                        code = process.wait()
                    finally: timer.cancel()
                    if code: raise ValueError(f'{command[0]} failed ({code}); inspect logs')
                job['status'] = 'passed'
            except Exception as exc:
                job['log'].append(str(exc)); job['status'] = 'failed'
            finally:
                job['finished_at'] = time.time()
                try: self.persist_job(job)
                finally:
                    self.active_job_id = None
                    self.lock.release()
        threading.Thread(target=task, daemon=True).start()
        return {'job_id': job['id']}

    def state(self):
        self.last_seen = time.monotonic()
        for role in self.roles(ctl.load()):
            lease = self.preview_root / role / 'config/lease'
            if lease.exists(): lease.touch()
        if not self.active_job_id:
            try:
                saved_job = json.loads((ROOT/'state/latest-job.json').read_text())
                if saved_job.get('status') == 'running':
                    try:
                        with ctl.control_lock():
                            # The worker may have completed between our first read and lock acquisition.
                            saved_job = json.loads((ROOT/'state/latest-job.json').read_text())
                            if not self.active_job_id and saved_job.get('status') == 'running':
                                saved_job['status'] = 'failed'
                                saved_job['finished_at'] = time.time()
                                saved_job['log'].append('Startup worker stopped before completion; inspect actual service state.')
                                self.persist_job(saved_job)
                    except RuntimeError: pass
                if not self.active_job_id: self.job = saved_job
            except (OSError, ValueError): pass
        data = ctl.load()
        try:
            snapshot = ctl.generation.load_current(ctl.ROOT)
            generated_current = bool(snapshot and not snapshot['draft'] and snapshot['inventory_sha256'] == ctl.digest(ctl.INV))
        except (OSError, ValueError, KeyError): generated_current = False
        result = {'generated_current': generated_current, 'inventory': data, 'revision': ctl.digest(ctl.INV), 'devices': self.devices(),
                  'platform': platform.system(), 'job': copy.deepcopy(self.job), 'previews': {}, 'services': [], 'docker_error': None}
        try:
            # One bulk query, without requiring a generated Compose file or opening camera devices.
            raw = execute(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={ctl.PROJECT}', '--format', '{{json .}}'], 5)
            result['services'] = [json.loads(line) for line in raw.splitlines() if line]
            for role in self.roles(ctl.load()):
                path = self.preview_root / role / 'out/status.json'
                item = self.managed(role)
                status = json.loads(path.read_text()) if path.exists() else {}
                result['previews'][role] = {'running': bool(item and item['State']['Running']), 'streams': status}
                if item and not item['State']['Running']:
                    result['previews'][role]['error'] = execute(['docker', 'logs', '--tail', '15', self.preview_names[role]], 5)
        except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            result['docker_error'] = str(exc)
        return result

    def lease_cleanup(self):
        while not self.closed.wait(5):
            if time.monotonic() - self.last_seen > 120 and self.lock.acquire(blocking=False):
                try:
                    with ctl.control_lock(): self.stop_previews()
                except Exception: pass
                finally: self.lock.release()

class Handler(BaseHTTPRequestHandler):
    server_version = 'CameraConsole/1.0'
    def send(self, code, body, content_type='application/json'):
        if not isinstance(body, bytes): body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers(); self.wfile.write(body)
    def allowed(self):
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        public_port = os.environ.get('ORBBEC_PUBLIC_PORT')
        if public_port and public_port.isdigit():
            allowed |= {f'127.0.0.1:{public_port}', f'localhost:{public_port}'}
        host = self.headers.get('Host', '')
        if host not in allowed: return False
        origin = self.headers.get('Origin')
        return not origin or origin == 'http://' + host
    def do_GET(self):
        if not self.allowed(): return self.send(403, {'error': 'Invalid host/origin'})
        path = urlparse(self.path)
        try:
            if path.path == '/healthz': return self.send(200, {'status': 'ok'})
            if path.path == '/api/state': return self.send(200, self.server.manager.state())
            if path.path == '/api/preview/logs':
                role = parse_qs(path.query).get('role', [''])[0]
                self.server.manager.role(ctl.load(), role)
                item = self.server.manager.managed(role)
                log = execute(['docker', 'logs', '--tail', '80', self.server.manager.preview_names[role]], 5) if item else ''
                return self.send(200, {'log': log})
            if path.path == '/api/frame':
                query = parse_qs(path.query); role = query.get('role', [''])[0]; stream = query.get('stream', [''])[0]
                self.server.manager.role(ctl.load(), role)
                if stream not in ('color', 'depth'): raise ValueError('Invalid stream')
                file = self.server.manager.preview_root / role / 'out' / (stream + '.jpg')
                if not file.exists() or time.time() - file.stat().st_mtime > 3:
                    return self.send(404, {'error': 'No fresh frame'})
                return self.send(200, file.read_bytes(), 'image/jpeg')
            files = {'/': ('index.html','text/html; charset=utf-8'), '/app.js': ('app.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8')}
            if path.path not in files: return self.send(404, {'error':'Not found'})
            file, mime = files[path.path]; body = (ROOT/'web'/file).read_bytes()
            if file == 'index.html': body = body.replace(b'__CSRF__', self.server.manager.token.encode())
            return self.send(200, body, mime)
        except Exception as exc: return self.send(400, {'error': str(exc)})
    def do_POST(self):
        manager = self.server.manager
        if not self.allowed() or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''), manager.token):
            return self.send(403, {'error':'Invalid origin or CSRF token'})
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 32768 or self.headers.get('Content-Type') != 'application/json':
                raise ValueError('Expected bounded JSON request')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict): raise ValueError('Expected JSON object')
            path = urlparse(self.path).path
            if path == '/api/job': return self.send(202, manager.start_job(payload.get('action')))
            if not manager.lock.acquire(blocking=False): raise ValueError('另一个操作正在运行。')
            try:
                with ctl.control_lock():
                    if path == '/api/config': result = manager.save_config(payload)
                    elif path == '/api/preview/start': result = manager.start_preview(payload.get('role'))
                    elif path == '/api/preview/stop':
                        manager.role(ctl.load(), payload.get('role')); manager.stop_preview(payload['role']); result = {'message':'预览已停止。'}
                    elif path == '/api/confirm': result = manager.confirm(payload.get('role'))
                    else: raise ValueError('Unknown action')
            finally: manager.lock.release()
            self.send(200, result)
        except Exception as exc: self.send(400, {'error':str(exc)})

def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--port', type=int, default=8765); parser.add_argument('--host', choices=['127.0.0.1','0.0.0.0'], default='127.0.0.1')
    args = parser.parse_args(); manager = Manager()
    if args.host == '0.0.0.0' and os.environ.get('ORBBEC_CONTAINERIZED') != '1':
        parser.error('0.0.0.0 is reserved for the loopback-published management container')
    if os.environ.get('ORBBEC_CONTAINERIZED') == '1' and Path(os.environ.get('ORBBEC_DEPLOY_DIR','')).resolve() != ROOT:
        parser.error('Deployment directory must have the identical host/container absolute path')
    server = ThreadingHTTPServer((args.host, args.port), Handler); server.manager = manager
    threading.Thread(target=manager.lease_cleanup, daemon=True).start()
    print(f'Camera console: http://127.0.0.1:{server.server_port}', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        manager.closed.set(); server.server_close()
        try: manager.stop_previews()
        except Exception: pass

if __name__ == '__main__': main()
