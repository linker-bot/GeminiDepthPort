"""Load per-device YAML and validate against the actual installed model launch file."""
import ast
import os
import json
import re
from pathlib import Path
import yaml
try:
    from .qos_contract import effective_driver_qos
except ImportError:
    from qos_contract import effective_driver_qos
from ament_index_python.packages import get_package_share_directory

MODELS = {
    'head': ('Gemini 335L', 'gemini_330_series.launch.py'),
    'wrist': ('Gemini 2', 'gemini2.launch.py'),
}

def read_parameters():
    role = os.environ['CAMERA_ROLE']
    name = os.environ['CAMERA_NAME']
    schema = os.environ.get('CAMERA_SCHEMA', '1')
    if schema == '2':
        binding = json.loads(Path('/config/binding.json').read_text())
        catalog = json.loads((Path(__file__).parent / 'driver_profiles.json').read_text())
        model = (os.environ['CAMERA_MODEL'], os.environ['CAMERA_LAUNCH'])
        allowed = {(p['model'], p['launch_file']) for p in catalog['profiles'].values()}
        namespace = os.environ['CAMERA_NAMESPACE']
        if (model not in allowed or binding['schema_version'] != 2
                or binding['frame_prefix'] != name or binding['namespace'] != namespace
                or (binding['model'], binding['launch_file']) != model
                or not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', name)
                or not namespace.startswith('/')
                or not all(re.fullmatch('[A-Za-z][A-Za-z0-9_]*', p) for p in namespace[1:].split('/'))):
            raise ValueError('Invalid explicit runtime binding')
    elif schema != '1':
        raise ValueError('Unsupported runtime schema')
    elif name != role + '_camera':
        raise ValueError('Namespace must be the role plus _camera')
    if schema == '1' and (os.environ['CAMERA_MODEL'], os.environ['CAMERA_LAUNCH']) != MODELS.get(role):
        raise ValueError('Model/launch file does not match fixed camera role')
    source = Path(get_package_share_directory('orbbec_camera')) / 'launch' / os.environ['CAMERA_LAUNCH']
    tree = ast.parse(source.read_text())
    keys = {n.args[0].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == 'DeclareLaunchArgument' and n.args
            and isinstance(n.args[0], ast.Constant)}
    params = yaml.safe_load(Path('/config/camera.yaml').read_text())
    unknown = set(params) - keys
    if unknown:
        raise ValueError(f'Unknown model launch parameters: {sorted(unknown)}')
    if params['camera_name'] != name:
        raise ValueError('camera_name and namespace differ')
    serial = params.get('serial_number')
    if not isinstance(serial, str) or not serial or serial.startswith('REPLACE_'):
        raise ValueError('An explicit string serial number is required')
    if not all(type(value) in (str, bool, int, float) for value in params.values()):
        raise ValueError('Only scalar launch argument values are supported')
    if schema == '2' and (params.get('color_format') != 'YUYV'
                          or params.get('depth_registration') is not False):
        raise ValueError('Runtime parameters violate raw image contract')
    if params.get('color_format') == 'YUYV' and any(params.get(key, False) for key in (
            'enable_colored_point_cloud', 'depth_registration', 'enable_color_undistortion',
            'color_flip', 'color_mirror', 'color_rotation', 'enable_enhanced_depth')):
        raise ValueError('Native YUYV processing combination is not validated')
    if schema == '2':
        params.update(effective_driver_qos(params))
    print('Runtime configuration accepted.', flush=True)
    return params

def launch_arguments(params):
    # Argument vector, never shell interpolation; spaces in depth_work_mode are preserved.
    return [f'{key}:={str(value).lower() if type(value) is bool else value}'
            for key, value in params.items()]

if __name__ == '__main__':
    read_parameters()
