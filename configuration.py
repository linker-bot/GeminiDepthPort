"""Offline camera schema validation. No hardware discovery or deployment defaults."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
from runtime.qos_contract import effective_driver_qos


class ConfigurationError(ValueError):
    """Stable category with a message that never includes input values."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(code + ': ' + message)


def require(condition, code, message):
    if not condition:
        raise ConfigurationError(code, message)


PROFILE_CATALOG = json.loads((Path(__file__).parent / 'driver_profiles.json').read_text())
MODEL_PROFILES = {
    role: (PROFILE_CATALOG['profiles'][profile]['model'], PROFILE_CATALOG['profiles'][profile]['launch_file'])
    for role, profile in (('head', 'gemini_335l'), ('wrist', 'gemini_2'))
}


def active(data):
    return [camera for camera in data['cameras'] if camera['enabled']]


def _validate_legacy(data, draft=False):
    require(data['schema_version'] == 1, 'CONFIG_INVALID', 'Unsupported schema')
    require(re.fullmatch('[0-9a-f]{40}', data['upstream_commit']), 'CONFIG_INVALID', 'Pin a full upstream commit')
    require(re.fullmatch('[a-zA-Z0-9._/:-]+', data['image']), 'CONFIG_INVALID', 'Invalid image name')
    require(type(data['ros_domain_id']) is int and 0 <= data['ros_domain_id'] <= 101, 'CONFIG_INVALID', 'Domain must be 0..101')
    require(30 <= data['startup_timeout_seconds'] <= 600, 'CONFIG_INVALID', 'Invalid startup timeout')
    require(len(data['cameras']) == 2 and {c['name'] for c in data['cameras']} == set(MODEL_PROFILES), 'CONFIG_INVALID', 'Maintain exactly head and wrist')
    require(len(active(data)) == 2, 'CONFIG_INVALID', 'Both head and wrist are required')
    (names, serials) = (set(), set())
    for c in data['cameras']:
        (name, sn) = (c['name'], c['serial_number'])
        require(re.fullmatch('[a-z][a-z0-9_]{0,31}', name) and name not in names, 'CONFIG_INVALID', 'Invalid/duplicate camera name')
        names.add(name)
        require(type(c['enabled']) is bool and type(c['profiles_verified']) is bool, 'CONFIG_INVALID', 'Use JSON booleans')
        require(re.fullmatch('[a-zA-Z0-9_]+\\.launch\\.py', c['launch_file']), 'CONFIG_INVALID', 'Invalid launch file')
        require((c['model'], c['launch_file']) == MODEL_PROFILES[name], 'CONFIG_INVALID', 'Model and launch file must match the camera role')
        p = c['parameters']
        require(all((type(v) in (bool, int, float, str) for v in p.values())), 'CONFIG_INVALID', 'Use scalar launch argument values')
        reserved = {'camera_name', 'serial_number', 'device_num', 'usb_port', 'config_file_path', 'load_config_json_file_path', 'net_device_ip', 'net_device_port', 'bag_filename'}
        require(not reserved.intersection(p), 'CONFIG_INVALID', 'Identity/source keys are managed by the tool')
        require(p.get('uvc_backend') == 'libuvc', 'CONFIG_INVALID', 'This USB template is for libuvc only')
        require(p.get('sync_mode') == 'standalone', 'CONFIG_INVALID', 'Hardware sync requires a separate design')
        require(p.get('depth_registration') is False, 'CONFIG_INVALID', 'This profile checks raw depth; D2C needs matching output probes')
        if name == 'head':
            require(p.get('enumerate_net_device') is False, 'CONFIG_INVALID', 'Network enumeration must be off')
        else:
            require('enumerate_net_device' not in p, 'CONFIG_INVALID', 'Gemini 2 launch does not expose network enumeration')
            require(p.get('enable_ir') is False, 'CONFIG_INVALID', 'Wrist IR must be disabled until probes are added')
        require(not any((p.get(k, False) for k in ('enable_left_ir', 'enable_right_ir', 'enable_accel', 'enable_gyro'))), 'CONFIG_INVALID', 'Add probes before enabling extra streams')
        require(any((p.get('enable_' + s, False) for s in ('color', 'depth'))), 'CONFIG_INVALID', 'At least one image stream required')
        for stream in ('color', 'depth'):
            require(type(p.get('enable_' + stream)) is bool, 'CONFIG_INVALID', 'Stream switches must be booleans')
            if p['enable_' + stream]:
                for suffix in ('width', 'height', 'fps'):
                    value = p[stream + '_' + suffix]
                    require(type(value) is int and value > 0, 'CONFIG_INVALID', 'Explicit positive stream profiles required')
        h = c['health']
        require(1 <= h['discovery_seconds'] <= 10 and 2 <= h['sample_seconds'] <= 10, 'CONFIG_INVALID', 'Probe must fit health timeout')
        require(0.1 <= h['min_fps_ratio'] <= 1 and 0 < h['freshness_seconds'] <= 5, 'CONFIG_INVALID', 'Invalid health thresholds')
        if c['enabled']:
            require(isinstance(sn, str) and sn and (sn not in serials), 'CONFIG_INVALID', 'Empty/duplicate serial number')
            serials.add(sn)
            if not draft:
                require(not sn.startswith('REPLACE_'), 'CONFIG_INVALID', f'{name}: bind the real serial number')
                require(c['profiles_verified'], 'CONFIG_INVALID', f'{name}: verify stream profile with hardware, then confirm-profile')


RESERVED = {'camera_name', 'serial_number', 'device_num', 'usb_port', 'config_file_path',
            'load_config_json_file_path', 'export_config_json_file_path', 'net_device_ip',
            'net_device_port', 'bag_filename', 'tf_prefix'}
CAMERA_FIELDS = {'name', 'profile', 'namespace', 'frame_prefix', 'serial_number', 'enabled',
                 'profiles_verified', 'verification', 'parameters', 'health',
                 'timestamp_source', 'calibration_revision', 'depth_unit'}
IDENTIFIER = re.compile(r'[a-z][a-z0-9_]{0,31}')
ROS_COMPONENT = re.compile(r'[A-Za-z][A-Za-z0-9_]*')


def camera_digest(data, camera):
    """Bind confirmation to the entire camera plus shared runtime configuration."""
    shared = {k: v for k, v in data.items() if k != 'cameras'}
    values = {k: v for k, v in camera.items() if k not in ('profiles_verified', 'verification')}
    raw = json.dumps({'shared': shared, 'camera': values}, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def invalidate_changed(previous, candidate):
    """Do not mutate caller data; only the changed camera loses confirmation."""
    result = copy.deepcopy(candidate)
    old = {c['name']: c for c in previous.get('cameras', [])}
    for camera in result['cameras']:
        before = old.get(camera['name'])
        if before is None or camera_digest(previous, before) != camera_digest(result, camera):
            camera['profiles_verified'] = False
            if result['schema_version'] == 2:
                camera['verification'] = None
    return result


def _validate_v2(data, draft):
    require(set(data) - {'communication'} == {'schema_version', 'image', 'upstream_commit', 'ros_domain_id',
                         'startup_timeout_seconds', 'cameras'},
            'CONFIG_FIELDS', 'Unexpected or missing inventory fields')
    require(isinstance(data['image'], str) and re.fullmatch(r'[a-zA-Z0-9._/:-]+', data['image']),
            'CONFIG_IMAGE', 'Invalid image reference')
    require(isinstance(data['upstream_commit'], str)
            and data['upstream_commit'] == PROFILE_CATALOG['upstream_commit'],
            'PROFILE_BASELINE', 'Driver baseline must match the public parameter catalog')
    require(type(data['ros_domain_id']) is int and 0 <= data['ros_domain_id'] <= 101,
            'CONFIG_DOMAIN', 'Invalid communication domain')
    require(type(data['startup_timeout_seconds']) is int
            and 30 <= data['startup_timeout_seconds'] <= 600,
            'CONFIG_TIMEOUT', 'Invalid startup timeout')
    require(data.get('communication', {'profile': 'humble-fastdds-local-udp-v1'})
            == {'profile': 'humble-fastdds-local-udp-v1'},
            'COMMUNICATION_PROFILE', 'Unsupported public runtime compatibility profile')
    cameras = data['cameras']
    require(isinstance(cameras, list) and len(cameras) <= 3,
            'CAMERA_COUNT', 'At most three configured cameras are supported')
    names, namespaces, frames, serials = set(), set(), set(), set()
    for camera in cameras:
        require(isinstance(camera, dict) and set(camera) == CAMERA_FIELDS,
                'CAMERA_FIELDS', 'Unexpected or missing camera fields')
        name = camera['name']
        require(isinstance(name, str) and IDENTIFIER.fullmatch(name) and name not in names,
                'CAMERA_ID', 'Invalid or duplicate logical identity')
        names.add(name)
        namespace, frame = camera['namespace'], camera['frame_prefix']
        require(isinstance(namespace, str) and namespace.startswith('/')
                and all(ROS_COMPONENT.fullmatch(v) for v in namespace[1:].split('/')),
                'CAMERA_NAMESPACE', 'Expected a canonical absolute ROS namespace')
        require(namespace not in namespaces
                and not any(namespace.startswith(v + '/') or v.startswith(namespace + '/') for v in namespaces),
                'CAMERA_NAMESPACE', 'Overlapping camera namespaces are not supported')
        namespaces.add(namespace)
        require(isinstance(frame, str) and ROS_COMPONENT.fullmatch(frame)
                and frame not in frames, 'CAMERA_FRAME', 'Invalid or duplicate frame prefix')
        frames.add(frame)
        require(type(camera['enabled']) is bool and type(camera['profiles_verified']) is bool,
                'CAMERA_BOOLEAN', 'Expected boolean enable and confirmation fields')
        profile = camera['profile']
        require(isinstance(profile, str) and profile in PROFILE_CATALOG['profiles'],
                'CAMERA_PROFILE', 'Unknown driver profile')
        serial = camera['serial_number']
        require(isinstance(serial, str) and (not serial or re.fullmatch(r'[A-Za-z0-9_-]+', serial)),
                'CAMERA_BINDING', 'Invalid device binding')
        placeholder = not serial or serial.upper().startswith(('REPLACE_', 'UNBOUND', 'PLACEHOLDER'))
        if camera['enabled']:
            require(not placeholder, 'CAMERA_UNBOUND', 'Enabled cameras need explicit device bindings')
            require(serial not in serials, 'CAMERA_BINDING', 'Duplicate enabled device binding')
            serials.add(serial)
        for field in ('timestamp_source', 'calibration_revision'):
            require(isinstance(camera[field], str), 'CAMERA_METADATA', 'Expected metadata revision strings')
        require(camera['depth_unit'] == 'mm', 'DEPTH_UNIT', 'Raw depth uses millimetres')
        params = camera['parameters']
        require(isinstance(params, dict) and all(isinstance(k, str) for k in params),
                'PARAMETER_FIELDS', 'Expected a parameter object')
        allowed = set(PROFILE_CATALOG['profiles'][profile]['parameters']) - RESERVED
        require(not (set(params) - allowed), 'PARAMETER_FIELDS', 'Unsupported or managed driver parameter')
        require(all(type(v) in (str, bool, int, float) and (type(v) is not float or math.isfinite(v))
                    for v in params.values()), 'PARAMETER_TYPE', 'Expected finite scalar parameters')
        require(type(params.get('enable_color')) is bool and type(params.get('enable_depth')) is bool,
                'STREAM_SWITCH', 'Explicit image stream switches are required')
        try:
            effective_driver_qos(params)
        except ValueError:
            raise ConfigurationError('QOS_CONTRACT', 'Image and calibration publishers require explicit reliable-compatible QoS') from None
        require(params.get('color_format') == 'YUYV', 'COLOR_FORMAT', 'Native YUYV is required')
        require(not any(params.get(key, False) for key in ('enable_color_undistortion', 'color_flip',
                'color_mirror', 'color_rotation', 'enable_enhanced_depth')),
                'COLOR_PROCESSING_UNSUPPORTED', 'Native YUYV processing combination is not validated')
        require(params.get('depth_format') == 'Y16' and params.get('depth_registration') is False,
                'DEPTH_FORMAT', 'Raw unregistered depth is required')
        require(not any(params.get(k, False) for k in
                        ('enable_left_ir', 'enable_right_ir', 'enable_ir', 'enable_accel', 'enable_gyro',
                         'enable_point_cloud', 'enable_colored_point_cloud')),
                'STREAM_UNSUPPORTED', 'Only contracted image streams are supported')
        if camera['enabled']:
            require(params['enable_color'] or params['enable_depth'], 'STREAM_EMPTY', 'Enable an image stream')
            require(bool(camera['timestamp_source']), 'CAMERA_TIME', 'Declare the capture time source')
            for stream in ('color', 'depth'):
                if params['enable_' + stream]:
                    require(all(type(params.get(stream + '_' + suffix)) is int
                                and params[stream + '_' + suffix] > 0 for suffix in ('width', 'height', 'fps')),
                            'STREAM_PROFILE', 'Enabled streams need explicit positive profiles')
            if params['enable_color']:
                require(params['color_width'] % 2 == 0, 'COLOR_WIDTH', 'YUYV width must be even')
        health = camera['health']
        require(isinstance(health, dict) and not (set(health) - {'discovery_seconds', 'sample_seconds',
                'freshness_seconds', 'min_fps_ratio'}), 'HEALTH_FIELDS', 'Invalid probe settings')
        if camera['enabled'] or health:
            require(set(health) == {'discovery_seconds', 'sample_seconds', 'freshness_seconds', 'min_fps_ratio'}
                    and all(type(v) in (int, float) and math.isfinite(v) for v in health.values()),
                    'HEALTH_FIELDS', 'Expected explicit finite probe settings')
            require(1 <= health['discovery_seconds'] <= 10 and 2 <= health['sample_seconds'] <= 10
                    and 0 < health['freshness_seconds'] <= 5 and 0.1 <= health['min_fps_ratio'] <= 1,
                    'HEALTH_RANGE', 'Probe settings outside supported limits')
        evidence = camera['verification']
        if camera['profiles_verified']:
            require(isinstance(evidence, dict) and set(evidence) == {'configuration_digest', 'method'}
                    and evidence['method'] == 'runtime-probe'
                    and evidence['configuration_digest'] == camera_digest(data, camera),
                    'CONFIRMATION_STALE', 'Confirmation must match the current configuration and probe')
        else:
            require(evidence is None, 'CONFIRMATION_STALE', 'Unconfirmed cameras cannot retain evidence')
        if camera['enabled'] and not draft:
            require(camera['profiles_verified'], 'PROFILE_UNVERIFIED', 'Actual profile verification required')
    if not draft:
        require(bool(active(data)), 'NO_ACTIVE_CAMERAS', 'No enabled cameras; not ready for capture')


def validate_inventory(data, draft=False):
    try:
        require(isinstance(data, dict) and type(data.get('schema_version')) is int,
                'CONFIG_SCHEMA', 'Expected a versioned inventory object')
        version = data['schema_version']
        require(version in (1, 2), 'CONFIG_SCHEMA', 'Unsupported inventory schema')
        if version == 1:
            _validate_legacy(data, draft)
        else:
            _validate_v2(data, draft)
    except ConfigurationError:
        raise
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ConfigurationError('CONFIG_SHAPE', 'Malformed inventory structure') from None


def migrate_legacy(data, bindings=None):
    """Pure preview: preserve known old bindings; never infer a third device."""
    validate_inventory(data, draft=True)
    require(data['schema_version'] == 1, 'MIGRATION_SCHEMA', 'Expected legacy inventory')
    require(bindings is None or isinstance(bindings, dict), 'MIGRATION_BINDING', 'Expected a binding map')
    bindings = bindings or {}
    old_names = {c['name'] for c in data['cameras']}
    require(not (set(bindings) - old_names), 'MIGRATION_BINDING', 'Unknown legacy logical identity')
    result = copy.deepcopy(data)
    result['schema_version'] = 2
    by_model = {p['model']: key for key, p in PROFILE_CATALOG['profiles'].items()}
    for camera in result['cameras']:
        old_name = camera['name']
        binding = bindings.get(old_name, {'name': old_name, 'namespace': '/' + old_name + '_camera',
                                         'frame_prefix': old_name + '_camera'})
        require(isinstance(binding, dict) and set(binding) == {'name', 'namespace', 'frame_prefix'},
                'MIGRATION_BINDING', 'Provide a complete explicit logical and ROS binding')
        camera.update(binding)
        camera['profile'] = by_model[camera.pop('model')]
        camera.pop('launch_file')
        camera.update(profiles_verified=False, verification=None, depth_unit='mm',
                      timestamp_source=camera['parameters'].get('time_domain', ''), calibration_revision='')
        camera['parameters']['color_format'] = 'YUYV'
        # Migration explicitly adopts the reliable publisher contract and clears
        # old confirmation above; it does not assert actual endpoint QoS.
        camera['parameters'].update(effective_driver_qos({}))
        # Placeholder devices stay unbound and disabled, never treated as real hardware.
        serial = camera['serial_number']
        if not serial or serial.upper().startswith(('REPLACE_', 'UNBOUND', 'PLACEHOLDER')):
            camera['serial_number'] = ''
            camera['enabled'] = False
    validate_inventory(result, draft=True)
    return result


def migration_plan_digest(source_bytes, candidate):
    payload = json.dumps(candidate, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(source_bytes + b'\0' + payload).hexdigest()


def require_legacy_runtime(data):
    """Schema v2 is an offline model until the runtime work packages land."""
    require(data.get('schema_version') == 1, 'RUNTIME_SCHEMA_PENDING',
            'Schema v2 deployment and preview adapters are not implemented yet')
