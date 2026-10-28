"""Synthetic offered policies; no endpoint names, identifiers or camera data."""
import legacy_support  # Enable the test-only Python behavior oracle.
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import configuration
import generation
from runtime.qos_contract import effective_driver_qos, validate_offered_qos, QOS_KEYS
from test_configuration_v2 import fixture, confirmed


class DefaultAndQoSTests(unittest.TestCase):
    def test_default_is_three_unbound_rgbd_slots_and_matches_example(self):
        data = json.loads((BASE / 'inventory.json').read_text())
        self.assertEqual(data, json.loads((BASE / 'inventory.v2.example.json').read_text()))
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(len(data['cameras']), 3)
        for camera in data['cameras']:
            self.assertFalse(camera['enabled'])
            self.assertFalse(camera['profiles_verified'])
            self.assertEqual(camera['serial_number'], '')
            self.assertTrue(camera['parameters']['enable_color'])
            self.assertTrue(camera['parameters']['enable_depth'])
            self.assertNotIn('color_width', camera['parameters'])
            self.assertEqual(set(effective_driver_qos(camera['parameters']).values()), {'DEFAULT'})
        configuration.validate_inventory(data, draft=True)
        with self.assertRaises(configuration.ConfigurationError):
            configuration.validate_inventory(data)

    def test_driver_profile_is_resolved_without_silent_fallback(self):
        self.assertEqual(effective_driver_qos({}), dict.fromkeys(QOS_KEYS, 'DEFAULT'))
        for key in QOS_KEYS:
            self.assertEqual(effective_driver_qos({key: 'default'})[key], 'DEFAULT')
            for value in ('SENSOR_DATA', 'SYSTEM_DEFAULT', 'RELIABLE', 'unknown', None, True):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    effective_driver_qos({key: value})

    def test_schema_rejects_unreliable_or_unknown_image_and_info_qos(self):
        for key in QOS_KEYS:
            data = fixture(); data['cameras'][0]['parameters'][key] = 'SENSOR_DATA'
            with self.assertRaises(configuration.ConfigurationError) as exc:
                configuration.validate_inventory(data, draft=True)
            self.assertEqual(exc.exception.code, 'QOS_CONTRACT')

    def test_omitted_qos_is_explicit_in_effective_driver_parameters(self):
        data = confirmed(fixture())
        for camera in data['cameras']:
            for key in QOS_KEYS: camera['parameters'].pop(key, None)
        for camera in generation.cameras(data):
            for key in QOS_KEYS: self.assertEqual(camera['parameters'][key], 'DEFAULT')

    def test_qos_change_invalidates_confirmation(self):
        before = confirmed(fixture()); after = copy.deepcopy(before)
        after['cameras'][0]['parameters']['color_qos'] = 'default'
        self.assertFalse(configuration.invalidate_changed(before, after)['cameras'][0]['profiles_verified'])

    def test_runtime_checks_discovered_policies_and_rejects_ambiguity(self):
        for publisher in ({'reliability': 'RELIABLE', 'durability': 'VOLATILE'},
                          SimpleNamespace(reliability=1, durability=2),
                          {'reliability': 1, 'durability': 1}):
            self.assertTrue(validate_offered_qos([publisher])['ready'])
        for policies in ([], [{'reliability':2,'durability':2}],
                         [{'reliability':0,'durability':2}],
                         [{'reliability':1,'durability':0}],
                         [{'reliability':True,'durability':2}],
                         [{'reliability':1,'durability':2}]*2):
            self.assertFalse(validate_offered_qos(policies)['ready'])
        with self.assertRaises(ValueError): validate_offered_qos([], required='unknown')


if __name__ == '__main__': unittest.main()
