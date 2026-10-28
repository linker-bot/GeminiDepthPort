"""Immutable, private deployment bundles with one atomic activation pointer."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import yaml
import configuration as config
from runtime.qos_contract import effective_driver_qos
from operation_guard import atomic_json

COMMUNICATION_PROFILE = 'humble-fastdds-local-udp-v1'
GENERATION_KIND = 'geminidepthport-generation-v1'


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cameras(data):
    result = []
    for camera in config.active(data):
        item = dict(camera)
        if data['schema_version'] == 1:
            item.update(namespace='/' + camera['name'] + '_camera', frame_prefix=camera['name'] + '_camera')
        else:
            item.update(config.PROFILE_CATALOG['profiles'][camera['profile']])
            item['parameters'] = dict(camera['parameters'], **effective_driver_qos(camera['parameters']))
        result.append(item)
    return sorted(result, key=lambda c: c['name'])


def communication(data):
    selected = data.get('communication', {'profile': COMMUNICATION_PROFILE})
    config.require(selected == {'profile': COMMUNICATION_PROFILE}, 'COMMUNICATION_PROFILE',
                   'Unsupported public runtime compatibility profile')
    return {'ROS_DOMAIN_ID': str(data['ros_domain_id']), 'ROS_LOCALHOST_ONLY': '1',
            'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
            'FASTRTPS_DEFAULT_PROFILES_FILE': '/app/fastdds.xml'}


def write_private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('x') as handle: handle.write(content)
    path.chmod(0o600)


def load_current(root, verify=True):
    root = Path(root)
    pointer = root / 'state/current-generation.json'
    if not pointer.exists(): return None
    try:
        current = json.loads(pointer.read_text())
        identifier = current['generation']
        config.require(isinstance(identifier, str) and re.fullmatch('[0-9a-f]{32}', identifier),
                       'GENERATION_INVALID', 'Invalid generation identifier')
        folder = root / 'state/generations' / identifier
        config.require(not folder.is_symlink(), 'GENERATION_INVALID', 'Generation must be a managed directory')
        manifest = folder / 'manifest.json'
        config.require(digest(manifest) == current['manifest_sha256'], 'GENERATION_DRIFT', 'Manifest changed')
        record = json.loads(manifest.read_text())
        config.require(record['kind'] == GENERATION_KIND and record['generation'] == identifier,
                       'GENERATION_INVALID', 'Invalid managed generation')
        if verify:
            for relative, expected in record['files'].items():
                path = Path(relative)
                config.require(not path.is_absolute() and '..' not in path.parts,
                               'GENERATION_INVALID', 'Invalid managed file reference')
                full = folder / path
                config.require(full.resolve().is_relative_to(folder.resolve()) if hasattr(Path, 'is_relative_to')
                               else str(full.resolve()).startswith(str(folder.resolve()) + os.sep),
                               'GENERATION_INVALID', 'Managed file escaped generation')
                config.require(digest(full) == expected, 'GENERATION_DRIFT', 'Generated content changed')
        return dict(record, folder=folder)
    except config.ConfigurationError: raise
    except (OSError, ValueError, KeyError, TypeError):
        raise config.ConfigurationError('GENERATION_INVALID', 'Cannot read active generation') from None


def create(ctl, data, draft=False):
    config.validate_inventory(data, draft=draft)
    communication(data)
    source = ctl.INV.read_bytes()
    config.require(json.loads(source) == data, 'INVENTORY_DRIFT', 'Reload saved inventory before generation')
    source_digest = hashlib.sha256(source).hexdigest()
    current = load_current(ctl.ROOT)
    if current and current['inventory_sha256'] == source_digest and current['draft'] == draft:
        return current
    ctl.assert_stopped()
    parent = ctl.ROOT / 'state/generations'; parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = secrets.token_hex(16)
    destination = parent / identifier
    stage = Path(tempfile.mkdtemp(prefix='.staging-', dir=parent))
    items = cameras(data)
    try:
        services = {'web': ctl.web_service()}
        previous = None
        for camera in items:
            name = camera['name']; service = 'camera_' + name
            params = dict(camera['parameters'], camera_name=camera['frame_prefix'],
                          serial_number=camera['serial_number'], device_num=len(items))
            if data['schema_version'] == 2:
                params.update(effective_driver_qos(params))
                params.update(enable_accel=False, enable_gyro=False, enable_point_cloud=False,
                              enable_colored_point_cloud=False)
                for key in ('enable_ir', 'enable_left_ir', 'enable_right_ir'):
                    if key in config.PROFILE_CATALOG['profiles'][camera['profile']]['parameters']:
                        params[key] = False
            binding = {'schema_version': data['schema_version'], 'namespace': camera['namespace'],
                       'frame_prefix': camera['frame_prefix'], 'model': camera['model'],
                       'launch_file': camera['launch_file'], 'contract': 'camera-interface-v1' if data['schema_version'] == 2 else 'legacy'}
            health = dict(camera['health'], camera_name=camera['frame_prefix'], streams=[])
            for stream in ('color', 'depth'):
                if params['enable_' + stream]:
                    spec = {'topic': camera['namespace'] + '/' + stream + '/image_raw',
                            'width': params[stream + '_width'], 'height': params[stream + '_height'],
                            'min_fps': params[stream + '_fps'] * health['min_fps_ratio']}
                    if data['schema_version'] == 2:
                        spec.update(encoding='yuyv' if stream == 'color' else '16UC1',
                                    frame_id=camera['frame_prefix'] + '_' + stream + '_optical_frame',
                                    required_reliability='reliable',
                                    camera_info_topic=camera['namespace'] + '/' + stream + '/camera_info')
                    health['streams'].append(spec)
            base = stage / 'config' / name
            write_private(base / 'camera.yaml', yaml.safe_dump(params, sort_keys=False))
            write_private(base / 'health.json', json.dumps(health, sort_keys=True))
            write_private(base / 'binding.json', json.dumps(binding, sort_keys=True))
            env = dict(communication(data), CAMERA_ROLE=name, CAMERA_NAME=camera['frame_prefix'],
                       CAMERA_NAMESPACE=camera['namespace'], CAMERA_SCHEMA=str(data['schema_version']),
                       CAMERA_MODEL=camera['model'], CAMERA_LAUNCH=camera['launch_file'])
            spec = ctl.camera_service(data, env, destination / 'config' / name)
            spec['labels'].update({'orbbec.generation': identifier, 'orbbec.camera': name})
            if previous: spec['depends_on'] = {previous: {'condition': 'service_healthy'}}
            services[service] = spec; previous = service
        write_private(stage / 'compose.yaml', yaml.safe_dump({'services': services}, sort_keys=False))
        files = {str(p.relative_to(stage)): digest(p) for p in stage.rglob('*') if p.is_file()}
        record = {'kind': GENERATION_KIND, 'generation': identifier, 'draft': draft,
                  'schema_version': data['schema_version'], 'inventory_sha256': source_digest,
                  'communication_profile': COMMUNICATION_PROFILE,
                  'cameras': [{'name': c['name'], 'service': 'camera_' + c['name']} for c in items],
                  'files': files}
        write_private(stage / 'manifest.json', json.dumps(record, sort_keys=True))
        ctl.validate_bundle(stage / 'compose.yaml')
        config.require(ctl.INV.read_bytes() == source, 'INVENTORY_DRIFT', 'Inventory changed during generation')
        stage.rename(destination)
        try:
            atomic_json(ctl.ROOT / 'state/current-generation.json',
                        {'generation': identifier, 'manifest_sha256': digest(destination / 'manifest.json')})
        except Exception:
            shutil.rmtree(destination)
            raise
        return dict(record, folder=destination)
    finally:
        if stage.exists(): shutil.rmtree(stage)
