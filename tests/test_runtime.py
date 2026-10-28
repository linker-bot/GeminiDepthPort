import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'runtime'))

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'launch').mkdir()
        self.data = json.loads((BASE / 'tests/fixtures/inventory.v1.json').read_text())
        contracts = json.loads((BASE / 'tests/launch-contracts.json').read_text())
        for c in self.data['cameras']:
            (self.root / 'launch' / c['launch_file']).write_text('\n'.join(f'DeclareLaunchArgument({key!r})' for key in contracts['keys'][c['name']]))
        ament = ModuleType('ament_index_python'); packages = ModuleType('ament_index_python.packages')
        packages.get_package_share_directory = lambda package: str(self.root)
        with patch.dict(sys.modules, {'ament_index_python': ament, 'ament_index_python.packages': packages}):
            spec = importlib.util.spec_from_file_location('validator', BASE / 'runtime/validate_runtime.py')
            self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
    def tearDown(self): self.tmp.cleanup()
    def read(self, role, extra=None):
        c = next(c for c in self.data['cameras'] if c['name'] == role)
        params = dict(c['parameters'], camera_name=role + '_camera', serial_number='SERIAL_1', device_num=2)
        if extra: params.update(extra)
        env = {'CAMERA_ROLE': role, 'CAMERA_NAME': role + '_camera', 'CAMERA_MODEL': c['model'], 'CAMERA_LAUNCH': c['launch_file']}
        original = self.module.Path.read_text
        def text(path, *args, **kwargs):
            return yaml.safe_dump(params) if str(path) == '/config/camera.yaml' else original(path, *args, **kwargs)
        with patch.dict(os.environ, env), patch.object(self.module.Path, 'read_text', text):
            return self.module.read_parameters()
    def test_both_models_load_yaml_to_launch_arguments(self):
        for role in ('head', 'wrist'):
            arguments = self.module.launch_arguments(self.read(role))
            self.assertIn('camera_name:=' + role + '_camera', arguments)
            self.assertIn('enable_point_cloud:=false', arguments)
            self.assertFalse(any(arg.startswith('config_file_path:=') for arg in arguments))
    def explicit(self, override_binding=None, override_params=None):
        camera = self.data['cameras'][1]
        binding = {'schema_version':2,'namespace':'/synthetic/preserved','frame_prefix':'synthetic_optical',
                   'model':camera['model'],'launch_file':camera['launch_file']}
        if override_binding:binding.update(override_binding)
        params = dict(camera['parameters'],camera_name='synthetic_optical',serial_number='SYNTHETIC_DEVICE',
                      device_num=3,color_format='YUYV',color_qos='DEFAULT',depth_qos='DEFAULT')
        if override_params:params.update(override_params)
        env={'CAMERA_SCHEMA':'2','CAMERA_ROLE':'logical_sensor','CAMERA_NAME':'synthetic_optical',
             'CAMERA_NAMESPACE':'/synthetic/preserved','CAMERA_MODEL':camera['model'],'CAMERA_LAUNCH':camera['launch_file']}
        original=self.module.Path.read_text
        def read(path,*args,**kwargs):
            if str(path)=='/config/camera.yaml':return yaml.safe_dump(params)
            if str(path)=='/config/binding.json':return json.dumps(binding)
            if path.name=='driver_profiles.json':return original(BASE/'driver_profiles.json')
            return original(path,*args,**kwargs)
        with patch.dict(os.environ,env),patch.object(self.module.Path,'read_text',read):
            return self.module.read_parameters()

    def test_explicit_namespace_and_frame_prefix_are_not_derived_from_role(self):
        params=self.explicit()
        self.assertEqual(params['camera_name'],'synthetic_optical')
        self.assertEqual(params['color_format'],'YUYV')

    def test_explicit_metadata_mismatch_and_format_downgrade_rejected(self):
        with self.assertRaises(ValueError):self.explicit(override_binding={'namespace':'/synthetic/wrong'})
        with self.assertRaises(ValueError):self.explicit(override_binding={'frame_prefix':'wrong'})
        with self.assertRaises(ValueError):self.explicit(override_params={'color_format':'MJPG'})

    def test_explicit_runtime_rejects_best_effort_and_invalid_profile_names(self):
        for value in ('SENSOR_DATA', 'SYSTEM_DEFAULT', 'RELIABLE'):
            with self.subTest(profile=value), self.assertRaises(ValueError):
                self.explicit(override_params={'color_qos': value})

    def test_work_mode_spaces_remain_one_argument(self):
        arguments = self.module.launch_arguments(self.read('wrist'))
        self.assertIn('depth_work_mode:=Unbinned Dense Default', arguments)
    def test_gemini2_rejects_unsupported_yaml_path_argument(self):
        with self.assertRaises(ValueError): self.read('wrist', {'config_file_path': '/config/ignored.yaml'})
    def test_namespace_mismatch_rejected(self):
        with self.assertRaises(ValueError): self.read('head', {'camera_name': 'wrist'})

if __name__ == '__main__': unittest.main()
