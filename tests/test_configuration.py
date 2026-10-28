import legacy_support  # Enable the test-only Python behavior oracle.
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ctl', BASE / 'tests/legacy_reference/cameractl.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((BASE / 'tests/fixtures/inventory.v1.json').read_text())
        for i, camera in enumerate(self.data['cameras']):
            camera['serial_number'] = f'TEST_SERIAL_{i}'
            camera['profiles_verified'] = True
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(ctl, 'ROOT', self.root), patch.object(ctl, 'INV', self.root / 'inventory.json'),
                        patch.object(ctl, 'assert_stopped'), patch.object(ctl, 'validate_bundle')]
        for p in self.patches: p.start()
        self.persist()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()
    @property
    def bundle(self): return ctl.generation.load_current(self.root)['folder']
    def persist(self): ctl.INV.write_text(json.dumps(self.data))
    def test_fixed_roles_models_and_namespace(self):
        ctl.render(self.data)
        compose = yaml.safe_load((self.bundle / 'compose.yaml').read_text())
        self.assertEqual(set(compose['services']), {'web', 'camera_head', 'camera_wrist'})
        for name in ('head', 'wrist'):
            params = yaml.safe_load((self.bundle / 'config' / name / 'camera.yaml').read_text())
            env = compose['services']['camera_' + name]['environment']
            self.assertEqual(params['camera_name'], env['CAMERA_NAME'])
            self.assertEqual(params['device_num'], 2)
            self.assertEqual((env['CAMERA_MODEL'], env['CAMERA_LAUNCH']), ctl.MODEL_PROFILES[name])
        ctl.check_render(self.data)
    def test_wrist_uses_400_pixel_depth_and_disables_ir(self):
        ctl.render(self.data)
        params = yaml.safe_load((self.bundle / 'config/wrist/camera.yaml').read_text())
        health = json.loads((self.bundle / 'config/wrist/health.json').read_text())
        self.assertEqual(params['depth_height'], 400)
        self.assertIs(params['enable_ir'], False)
        self.assertNotIn('enumerate_net_device', params)
        self.assertEqual(next(s['height'] for s in health['streams'] if '/depth/' in s['topic']), 400)
    def test_disabled_color_removed_from_probe(self):
        self.data['cameras'][1]['parameters']['enable_color'] = False
        self.persist(); ctl.render(self.data)
        health = json.loads((self.bundle / 'config/wrist/health.json').read_text())
        self.assertEqual([s['topic'] for s in health['streams']], ['/wrist_camera/depth/image_raw'])
    def test_duplicate_serial_rejected(self):
        self.data['cameras'][1]['serial_number'] = self.data['cameras'][0]['serial_number']
        with self.assertRaises(ctl.ConfigurationError): ctl.validate(self.data)
    def test_unverified_and_placeholder_rejected(self):
        for key, value in [('profiles_verified', False), ('serial_number', 'REPLACE_HEAD')]:
            data = copy.deepcopy(self.data); data['cameras'][0][key] = value
            with self.assertRaises(ctl.ConfigurationError): ctl.validate(data)
    def test_identity_override_rejected(self):
        self.data['cameras'][0]['parameters']['serial_number'] = 'wrong'
        with self.assertRaises(ctl.ConfigurationError): ctl.validate(self.data)
    def test_draft_and_config_drift_rejected(self):
        ctl.render(self.data, draft=True)
        with self.assertRaises(ctl.ConfigurationError): ctl.check_render(self.data)
        ctl.render(self.data)
        f = self.bundle / 'config/head/camera.yaml'; f.write_text(f.read_text() + '\n# drift\n')
        with self.assertRaises(ctl.ConfigurationError): ctl.check_render(self.data)
    def test_inventory_drift_rejected(self):
        ctl.render(self.data); ctl.INV.write_text(ctl.INV.read_text() + '\n')
        with self.assertRaises(ctl.ConfigurationError): ctl.check_render(self.data)
    def test_wrong_model_or_launch_rejected(self):
        for key, value in [('model', 'Gemini 2 L'), ('launch_file', 'gemini2L.launch.py')]:
            data = copy.deepcopy(self.data); data['cameras'][1][key] = value
            with self.assertRaises(ctl.ConfigurationError): ctl.validate(data)
    def test_extra_or_missing_role_rejected(self):
        data = copy.deepcopy(self.data); data['cameras'].pop()
        with self.assertRaises(ctl.ConfigurationError): ctl.validate(data)
        self.data['cameras'][1]['enabled'] = False
        with self.assertRaises(ctl.ConfigurationError): ctl.validate(self.data)
    def test_both_generated_profiles_use_declared_model_parameters(self):
        ctl.render(self.data)
        contracts = json.loads((BASE / 'tests/launch-contracts.json').read_text())
        self.assertEqual(contracts['upstream_commit'], self.data['upstream_commit'])
        for name in ('head', 'wrist'):
            params = yaml.safe_load((self.bundle / 'config' / name / 'camera.yaml').read_text())
            self.assertFalse(set(params) - set(contracts['keys'][name]))

if __name__ == '__main__': unittest.main()
