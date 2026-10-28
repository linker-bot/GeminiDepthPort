"""Synthetic three-camera Web integration; no real device identities or capture."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import cameractl as ctl
import web_server as web
from test_configuration_v2 import fixture


class ThreeCameraWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = fixture()
        self.patches = [patch.object(ctl, 'ROOT', self.root), patch.object(ctl, 'INV', self.root/'inventory.json'),
                        patch.object(web, 'ROOT', self.root)]
        for p in self.patches: p.start()
        ctl._atomic_inventory(self.data)
        self.manager = web.Manager()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_all_three_cameras_edit_and_reject_forged_confirmation(self):
        payload = {'revision': ctl.digest(ctl.INV), 'cameras': [
            {'name': c['name'], 'parameters': {'color_fps': 4}} for c in self.data['cameras']]}
        with patch.object(self.manager, 'devices', return_value=[]), patch.object(self.manager, 'managed', return_value=None):
            self.manager.save_config(payload)
        self.assertEqual([c['parameters']['color_fps'] for c in ctl.load()['cameras']], [4,4,4])
        payload['revision'] = ctl.digest(ctl.INV)
        payload['cameras'][0]['profiles_verified'] = True
        with self.assertRaises(ValueError): self.manager.save_config(payload)

    def test_preview_uses_explicit_binding_and_real_probe_confirmation(self):
        camera = self.data['cameras'][2]
        role = camera['name']
        devices = [{'serial_number': camera['serial_number'], 'model': 'Gemini 2'}]
        labels = {'org.geminidepthport.native-yuyv': '1', 'org.geminidepthport.explicit-bindings': '1',
                  'org.geminidepthport.native-yuyv-patch': ctl.driver_build.patch_identity()['patch_sha256'],
                  'org.opencontainers.image.revision': self.data['upstream_commit']}
        with patch.object(self.manager, 'devices', return_value=devices), \
             patch.object(self.manager, 'formal_running', return_value=False), \
             patch.object(self.manager, 'stop_preview'), patch.object(web, 'execute') as execute, \
             patch.object(ctl, 'run', return_value=json.dumps([{'Config': {'Labels': labels}}])):
            self.manager.start_preview(role)
            args = execute.call_args.args[0]
            self.assertIn('CAMERA_SCHEMA=2', args)
            self.assertIn('CAMERA_NAMESPACE='+camera['namespace'], args)
        folder = self.manager.preview_root/role
        health = json.loads((folder/'config/health.json').read_text())
        self.assertEqual(len(health['streams']),2)
        self.assertEqual(health['streams'][0]['required_reliability'],'reliable')
        self.assertEqual(json.loads((folder/'config/binding.json').read_text())['namespace'],camera['namespace'])
        status = {stream: {'received_at':time.time(),'frames':4,
                          'width':camera['parameters'][stream+'_width'],
                          'height':camera['parameters'][stream+'_height']} for stream in ('color','depth')}
        (folder/'out/status.json').write_text(json.dumps(status))
        with patch.object(self.manager, 'managed', return_value={'State':{'Running':True}}), \
             patch.object(web, 'execute', side_effect=ValueError('synthetic failure')):
            with self.assertRaises(ValueError): self.manager.confirm(role)
        self.assertFalse(ctl.load()['cameras'][2]['profiles_verified'])
        with patch.object(self.manager, 'managed', return_value={'State':{'Running':True}}), patch.object(web, 'execute'):
            self.manager.confirm(role)
        confirmed = ctl.load()['cameras'][2]
        self.assertTrue(confirmed['profiles_verified'])
        self.assertEqual(confirmed['verification']['configuration_digest'],ctl.config.camera_digest(ctl.load(),confirmed))

    def test_preview_refuses_uncontracted_image_before_docker_run(self):
        camera = self.data['cameras'][0]
        with patch.object(self.manager,'devices',return_value=[{'serial_number':camera['serial_number'],'model':'Gemini 335L'}]), \
             patch.object(self.manager,'formal_running',return_value=False), patch.object(web,'execute') as execute, \
             patch.object(ctl,'run',return_value='[{"Config":{"Labels":{}}}]'):
            with self.assertRaises(ctl.ConfigurationError): self.manager.start_preview(camera['name'])
        execute.assert_not_called()
