"""Synthetic schema and migration cases; no device or consumer configuration."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import configuration as cfg
import cameractl as ctl
import web_server as web
import deployment_control as deployment


def fixture():
    data = json.loads((BASE / 'inventory.v2.example.json').read_text())
    for index, camera in enumerate(data['cameras']):
        camera.update(enabled=True, serial_number='SYNTHETIC_' + str(index),
                      timestamp_source='synthetic-clock', calibration_revision='synthetic-calibration')
        camera['parameters'].update(color_width=4, color_height=2, color_fps=3,
                                    enable_depth=True, depth_width=2, depth_height=2, depth_fps=3)
        camera['health'] = dict(discovery_seconds=1, sample_seconds=2,
                                freshness_seconds=1, min_fps_ratio=0.5)
    return data


def confirmed(data):
    data = copy.deepcopy(data)
    for camera in data['cameras']:
        camera['profiles_verified'] = True
        camera['verification'] = {'method': 'runtime-probe',
                                  'configuration_digest': cfg.camera_digest(data, camera)}
    return data


class SchemaTests(unittest.TestCase):
    def setUp(self): self.data = fixture()

    def reject(self, data, code):
        with self.assertRaises(cfg.ConfigurationError) as exc:
            cfg.validate_inventory(data, draft=True)
        self.assertEqual(exc.exception.code, code)

    def test_one_two_three_enabled_cameras_and_reused_model(self):
        for count in (1, 2, 3):
            data = fixture()
            for camera in data['cameras'][count:]: camera['enabled'] = False
            cfg.validate_inventory(data, draft=True)
            cfg.validate_inventory(confirmed(data))
        self.assertEqual(self.data['cameras'][1]['profile'], self.data['cameras'][2]['profile'])

    def test_four_cameras_rejected(self):
        self.data['cameras'].append(copy.deepcopy(self.data['cameras'][0]))
        self.reject(self.data, 'CAMERA_COUNT')

    def test_zero_enabled_is_editable_but_not_ready(self):
        for cameras in ([], json.loads((BASE / 'inventory.v2.example.json').read_text())['cameras']):
            self.data['cameras'] = cameras
            cfg.validate_inventory(self.data, draft=True)
            with self.assertRaises(cfg.ConfigurationError) as exc: cfg.validate_inventory(self.data)
            self.assertEqual(exc.exception.code, 'NO_ACTIVE_CAMERAS')

    def test_duplicate_and_unbound_devices_rejected_even_in_draft(self):
        for serial, code in [('', 'CAMERA_UNBOUND'), ('REPLACE_DEVICE', 'CAMERA_UNBOUND'),
                             ('SYNTHETIC_0', 'CAMERA_BINDING')]:
            data = fixture(); data['cameras'][1]['serial_number'] = serial
            self.reject(data, code)

    def test_duplicate_identity_namespace_and_frame_rejected(self):
        for field, code in [('name', 'CAMERA_ID'), ('namespace', 'CAMERA_NAMESPACE'),
                            ('frame_prefix', 'CAMERA_FRAME')]:
            data = fixture(); data['cameras'][1][field] = data['cameras'][0][field]
            self.reject(data, code)

    def test_invalid_and_overlapping_namespace_rejected(self):
        for namespace in ('relative', '/', '/a/', '/a//b', '/9invalid', '/camera_a/child'):
            data = fixture(); data['cameras'][1]['namespace'] = namespace
            self.reject(data, 'CAMERA_NAMESPACE')

    def test_identity_independent_of_model_namespace_and_frames(self):
        camera = self.data['cameras'][1]
        camera.update(name='sensor_x', namespace='/synthetic/sensor', frame_prefix='optical_x')
        cfg.validate_inventory(self.data, draft=True)

    def test_unknown_profile_and_arbitrary_launch_rejected(self):
        self.data['cameras'][0]['profile'] = 'arbitrary.launch.py'
        self.reject(self.data, 'CAMERA_PROFILE')
        data = fixture(); data['cameras'][0]['launch_file'] = 'arbitrary.launch.py'
        self.reject(data, 'CAMERA_FIELDS')

    def test_model_specific_parameters_are_not_interchangeable(self):
        self.data['cameras'][1]['parameters']['enumerate_net_device'] = False
        self.reject(self.data, 'PARAMETER_FIELDS')

    def test_managed_source_fields_cannot_be_injected(self):
        for field in cfg.RESERVED:
            data = fixture(); data['cameras'][0]['parameters'][field] = 'PRIVATE_SENTINEL'
            self.reject(data, 'PARAMETER_FIELDS')

    def test_color_format_never_falls_back(self):
        for value in ('MJPG', 'RGB', 'ANY', 'yuyv'):
            data = fixture(); data['cameras'][0]['parameters']['color_format'] = value
            self.reject(data, 'COLOR_FORMAT')

    def test_odd_color_width_rejected(self):
        self.data['cameras'][0]['parameters']['color_width'] = 3
        self.reject(self.data, 'COLOR_WIDTH')

    def test_depth_must_be_raw_millimetres(self):
        for field, value, code in [('depth_unit', 'm', 'DEPTH_UNIT')]:
            data = fixture(); data['cameras'][0][field] = value
            self.reject(data, code)
        for key, value in [('depth_registration', True), ('depth_format', 'ANY')]:
            data = fixture(); data['cameras'][0]['parameters'][key] = value
            self.reject(data, 'DEPTH_FORMAT')

    def test_disabled_stream_needs_no_dimensions(self):
        camera = self.data['cameras'][0]; camera['parameters']['enable_depth'] = False
        for suffix in ('width', 'height', 'fps'): camera['parameters'].pop('depth_' + suffix)
        cfg.validate_inventory(self.data, draft=True)

    def test_bad_shapes_values_and_numbers_have_stable_errors(self):
        for value in (None, [], {}, {'schema_version': True}, {'schema_version': 99}):
            with self.assertRaises(cfg.ConfigurationError): cfg.validate_inventory(value)
        for value in (None, True, 0, -1, float('nan')):
            data = fixture(); data['cameras'][0]['parameters']['color_fps'] = value
            with self.assertRaises(cfg.ConfigurationError): cfg.validate_inventory(data, draft=True)

    def test_boolean_confirmation_without_evidence_is_rejected(self):
        self.data['cameras'][0]['profiles_verified'] = True
        self.reject(self.data, 'CONFIRMATION_STALE')

    def test_confirmation_invalidated_by_every_binding_and_geometry_change(self):
        for field, value in [('serial_number', 'SYNTHETIC_NEW'), ('namespace', '/synthetic/new'),
                             ('frame_prefix', 'new_optical'), ('profile', 'gemini_2'),
                             ('timestamp_source', 'new-clock'), ('calibration_revision', 'new-calibration')]:
            before = confirmed(fixture()); after = copy.deepcopy(before)
            after['cameras'][0][field] = value
            changed = cfg.invalidate_changed(before, after)
            self.assertFalse(changed['cameras'][0]['profiles_verified'])
            self.assertIsNone(changed['cameras'][0]['verification'])
            self.assertTrue(changed['cameras'][1]['profiles_verified'])
            self.assertTrue(before['cameras'][0]['profiles_verified'])
        before = confirmed(fixture()); after = copy.deepcopy(before)
        after['cameras'][0]['parameters']['color_width'] += 2
        self.assertFalse(cfg.invalidate_changed(before, after)['cameras'][0]['profiles_verified'])

    def test_global_runtime_change_invalidates_all_confirmations(self):
        before = confirmed(fixture()); after = copy.deepcopy(before)
        after['image'] += '-synthetic'
        self.assertTrue(all(not c['profiles_verified'] for c in cfg.invalidate_changed(before, after)['cameras']))

    def test_stale_evidence_cannot_pass_validation(self):
        data = confirmed(self.data); data['cameras'][0]['parameters']['color_width'] += 2
        self.reject(data, 'CONFIRMATION_STALE')

    def test_error_does_not_echo_input_values(self):
        self.data['cameras'][0]['namespace'] = 'PRIVATE_SENTINEL'
        with self.assertRaises(cfg.ConfigurationError) as exc: cfg.validate_inventory(self.data, draft=True)
        self.assertNotIn('PRIVATE_SENTINEL', str(exc.exception))

    def test_validation_survives_python_optimization(self):
        code = "import configuration as c; c.validate_inventory({'schema_version':2}, draft=True)"
        result = subprocess.run([sys.executable, '-O', '-c', code], cwd=BASE, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CONFIG_FIELDS', result.stderr)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.legacy = json.loads((BASE / 'tests/fixtures/inventory.v1.json').read_text())
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.inventory = self.root / 'inventory.json'
        self.inventory.write_text(json.dumps(self.legacy))
        self.patches = [patch.object(ctl, 'ROOT', self.root), patch.object(ctl, 'INV', self.inventory)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_preview_preserves_bindings_does_not_invent_third_device(self):
        previous = copy.deepcopy(self.legacy)
        result = cfg.migrate_legacy(self.legacy)
        self.assertEqual(self.legacy, previous)
        self.assertEqual(len(result['cameras']), 2)
        for old, new in zip(self.legacy['cameras'], result['cameras']):
            self.assertEqual(old['name'], new['name'])
            self.assertEqual(new['namespace'], '/' + old['name'] + '_camera')
            self.assertEqual(new['frame_prefix'], old['name'] + '_camera')
            self.assertFalse(new['profiles_verified']); self.assertIsNone(new['verification'])
            self.assertEqual(new['parameters']['color_format'], 'YUYV')
            self.assertFalse(new['enabled']); self.assertEqual(new['serial_number'], '')

    def test_explicit_remap_requires_complete_nonconflicting_bindings(self):
        name = self.legacy['cameras'][0]['name']
        binding = dict(name='sensor_x', namespace='/synthetic/preserved', frame_prefix='synthetic_optical')
        result = cfg.migrate_legacy(self.legacy, {name: binding})
        for field in binding: self.assertEqual(result['cameras'][0][field], binding[field])
        for bindings in ({'unknown': binding}, {name: {'name': 'sensor_x'}}, {name: []}):
            with self.assertRaises(cfg.ConfigurationError): cfg.migrate_legacy(self.legacy, bindings)

    def test_known_synthetic_binding_preserved_but_confirmation_cleared(self):
        for i, c in enumerate(self.legacy['cameras']):
            c['serial_number'] = 'SYNTHETIC_' + str(i); c['profiles_verified'] = True
        result = cfg.migrate_legacy(self.legacy)
        for old, new in zip(self.legacy['cameras'], result['cameras']):
            self.assertEqual(new['serial_number'], old['serial_number'])
            self.assertTrue(new['enabled']); self.assertFalse(new['profiles_verified'])

    def test_preview_has_no_side_effects_and_public_output_contains_no_bindings(self):
        before = self.inventory.read_bytes()
        result = ctl.migrate_inventory()
        self.assertEqual(self.inventory.read_bytes(), before)
        self.assertFalse((self.root / 'state').exists())
        self.assertFalse(result['applied']); self.assertFalse(result['runtime_supported'])
        self.assertNotIn('serial_number', json.dumps(result)); self.assertNotIn('parameters', json.dumps(result))

    def test_apply_requires_exact_preview_and_stopped_drivers(self):
        before = self.inventory.read_bytes(); plan = ctl.migrate_inventory()['plan_digest']
        with self.assertRaises(cfg.ConfigurationError): ctl.migrate_inventory(apply=True)
        with patch.object(ctl, 'assert_stopped', side_effect=cfg.ConfigurationError('CAMERAS_RUNNING', 'busy')):
            with self.assertRaises(cfg.ConfigurationError): ctl.migrate_inventory(apply=True, expected_plan=plan)
        self.assertEqual(self.inventory.read_bytes(), before)
        self.assertFalse((self.root / 'state/migrations').exists())

    def test_changed_source_or_mapping_rejects_stale_preview(self):
        plan = ctl.migrate_inventory()['plan_digest']
        self.inventory.write_bytes(self.inventory.read_bytes() + b'\n')
        with self.assertRaises(cfg.ConfigurationError): ctl.migrate_inventory(apply=True, expected_plan=plan)
        plan = ctl.migrate_inventory()['plan_digest']
        name = self.legacy['cameras'][0]['name']
        binding = {name: dict(name='sensor_x', namespace='/synthetic/remap', frame_prefix='synthetic_optical')}
        with self.assertRaises(cfg.ConfigurationError):
            ctl.migrate_inventory(binding, apply=True, expected_plan=plan)

    def test_apply_creates_private_exact_backup_before_replacement(self):
        before = self.inventory.read_bytes(); plan = ctl.migrate_inventory()['plan_digest']
        with patch.object(ctl, 'assert_stopped'):
            result = ctl.migrate_inventory(apply=True, expected_plan=plan)
        self.assertTrue(result['applied']); self.assertTrue(result['backup_created'])
        backup, = (self.root / 'state/migrations').glob('*.json')
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.inventory.stat().st_mode & 0o777, 0o600)
        cfg.validate_inventory(ctl.load(), draft=True)

    def test_inventory_failure_keeps_original_and_backup(self):
        before = self.inventory.read_bytes(); plan = ctl.migrate_inventory()['plan_digest']
        with patch.object(ctl, 'assert_stopped'), patch.object(ctl.os, 'replace', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError): ctl.migrate_inventory(apply=True, expected_plan=plan)
        self.assertEqual(self.inventory.read_bytes(), before)
        self.assertEqual(len(list((self.root/'state/migrations').glob('*.json'))), 1)
        self.assertFalse(list(self.root.glob('.inventory-*')))

    def test_failed_backup_never_replaces_inventory(self):
        before = self.inventory.read_bytes(); plan = ctl.migrate_inventory()['plan_digest']
        with patch.object(ctl, 'assert_stopped'), patch.object(ctl.tempfile, 'mkstemp', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError): ctl.migrate_inventory(apply=True, expected_plan=plan)
        self.assertEqual(self.inventory.read_bytes(), before)

    def test_saving_changes_invalidates_verification_and_blocks_direct_confirmation(self):
        data = confirmed(fixture()); ctl._atomic_inventory(data)
        data['cameras'][0]['parameters']['color_width'] += 2
        ctl.save(data)
        saved = ctl.load()
        self.assertFalse(saved['cameras'][0]['profiles_verified'])
        self.assertTrue(saved['cameras'][1]['profiles_verified'])
        before = self.inventory.read_bytes()
        with self.assertRaises(cfg.ConfigurationError): ctl.save(confirmed(saved))
        self.assertEqual(self.inventory.read_bytes(), before)

    def test_model_catalog_matches_public_launch_contract(self):
        contracts = json.loads((BASE / 'tests/launch-contracts.json').read_text())
        self.assertEqual(cfg.PROFILE_CATALOG['upstream_commit'], contracts['upstream_commit'])
        for legacy_role, profile in [('head', 'gemini_335l'), ('wrist', 'gemini_2')]:
            self.assertEqual(set(cfg.PROFILE_CATALOG['profiles'][profile]['parameters']),
                             set(contracts['keys'][legacy_role]))

    def test_legacy_save_invalidates_only_changed_camera(self):
        for i, camera in enumerate(self.legacy['cameras']):
            camera.update(serial_number='SYNTHETIC_' + str(i), profiles_verified=True)
        ctl._atomic_inventory(self.legacy)
        changed = copy.deepcopy(self.legacy)
        changed['cameras'][0]['parameters']['color_fps'] += 1
        ctl.save(changed)
        self.assertFalse(ctl.load()['cameras'][0]['profiles_verified'])
        self.assertTrue(ctl.load()['cameras'][1]['profiles_verified'])

    def test_noop_cli_edit_preserves_valid_evidence(self):
        data = confirmed(fixture()); ctl._atomic_inventory(data)
        camera = data['cameras'][0]
        with patch.object(sys, 'argv', ['cameractl.py', 'bind', camera['name'], camera['serial_number']]):
            ctl.main()
        self.assertTrue(ctl.load()['cameras'][0]['profiles_verified'])

    def test_optimized_mode_cannot_skip_running_camera_guard(self):
        code = """from unittest.mock import patch
import cameractl as ctl
with patch.object(ctl.lifecycle, 'instances', return_value=[{'State': {'Running': True}}]):
    ctl.assert_stopped()
"""
        result = subprocess.run([sys.executable, '-O', '-c', code], cwd=BASE, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CAMERAS_RUNNING', result.stderr)

    def test_v2_boot_refuses_before_any_stop_or_start(self):
        ctl._atomic_inventory(confirmed(fixture()))
        ctl.guard.acquire(self.root, 'synthetic-generation')
        with patch.object(deployment, 'Manager') as manager, patch.object(ctl, 'stop_cameras') as stop:
            with self.assertRaises(cfg.ConfigurationError): deployment.start('boot')
            stop.assert_not_called(); manager.assert_not_called()

    def test_cli_confirmation_uses_live_probe_instead_of_setting_flag(self):
        before = self.inventory.read_bytes()
        with patch.object(sys, 'argv', ['cameractl.py', 'confirm-profile', 'head']), \
             patch.object(web.Manager, 'confirm', side_effect=ValueError('synthetic probe failure')) as probe:
            with self.assertRaises(ValueError): ctl.main()
            probe.assert_called_once_with('head')
        self.assertEqual(self.inventory.read_bytes(), before)

    def test_v2_cannot_enter_legacy_render_start_or_preview(self):
        data = fixture(); ctl._atomic_inventory(data)
        with patch.object(ctl, 'compose') as command:
            for operation in (lambda: ctl.start_cameras(data),):
                with self.assertRaises(cfg.ConfigurationError): operation()
            command.assert_not_called()
        for camera in data['cameras']: camera['enabled'] = False
        ctl._atomic_inventory(data)
        with patch.object(web, 'ROOT', self.root), patch.object(web, 'execute') as execute:
            manager = web.Manager()
            with self.assertRaises(ValueError): manager.start_preview('camera_a')
            with self.assertRaises(FileNotFoundError): manager.confirm('camera_a')
            with self.assertRaises(ValueError): manager.save_config({})
            execute.assert_not_called()

    def test_invalid_legacy_input_has_explicit_error(self):
        self.legacy['cameras'][0]['parameters'] = None
        with self.assertRaises(cfg.ConfigurationError): cfg.migrate_legacy(self.legacy)


if __name__ == '__main__': unittest.main()
