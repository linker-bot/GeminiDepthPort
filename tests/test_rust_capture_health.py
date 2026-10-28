"""Synthetic status/source-time failures; no SDK, USB or ROS runtime required."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from runtime.source_time import host_stamp_fresh
spec = importlib.util.spec_from_file_location('rust_capture_health_wrapper', BASE / 'scripts/capture-runtime/health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class RustCaptureHealthTests(unittest.TestCase):
    def setUp(self):
        self.config = json.dumps({'service': {'generation': 'synthetic-generation', 'heartbeat_timeout_ms': 10000, 'idle_timeout_ms': 2000}}).encode()
        self.status = {'version': 1, 'kind': 'capture-service-status', 'ready': True, 'state': 'ready',
                       'generation': 'synthetic-generation', 'config_sha256': hashlib.sha256(self.config).hexdigest(),
                       'session_id': 'a' * 32, 'pid': 42, 'heartbeat_sequence': 10,
                       'updated_unix_ms': 100000, 'last_pair_age_ms': 10}

    def validate(self, status, now=100100, alive=True, parent=True):
        return health.validate_snapshot(self.config, status, now, alive=lambda p: alive and p == 42, parent_alive=lambda p: parent)

    def test_fresh_generation_session_config_and_process_are_required(self):
        self.validate(self.status)
        for field, value in [('ready', False), ('state', 'degraded'), ('kind', 'other'), ('version', True),
                             ('generation', 'other'), ('config_sha256', 'b' * 64), ('session_id', 'bad'),
                             ('heartbeat_sequence', 0), ('last_pair_age_ms', None), ('pid', 99)]:
            with self.subTest(field=field):
                status = dict(self.status, **{field: value})
                with self.assertRaises(ValueError): self.validate(status)
        with self.assertRaises(ValueError): self.validate(self.status, alive=False)
        with self.assertRaises(ValueError): self.validate(self.status, parent=False)

    def test_old_ready_cannot_hide_old_heartbeat_or_last_pair(self):
        with self.assertRaises(ValueError): self.validate(self.status, now=111000)
        with self.assertRaises(ValueError): self.validate(self.status, now=102000)
        with self.assertRaises(ValueError): self.validate(dict(self.status, last_pair_age_ms=2001))
        with self.assertRaises(ValueError): self.validate(self.status, now=99000)

    def test_session_change_and_frozen_heartbeat_fail_after_ros_probe(self):
        following = dict(self.status, heartbeat_sequence=11)
        health.validate_transition(self.status, following)
        with self.assertRaises(ValueError): health.validate_transition(self.status, self.status)
        with self.assertRaises(ValueError): health.validate_transition(self.status, dict(following, session_id='b' * 32))
        with self.assertRaises(ValueError): health.validate_transition(self.status, dict(following, pid=43))

    def test_host_source_time_rejects_stale_future_zero_and_bad_nsec(self):
        now = 1_700_000_000_500_000_000
        self.assertTrue(host_stamp_fresh(1_700_000_000, 400_000_000, now, 1))
        self.assertFalse(host_stamp_fresh(1_699_999_998, 0, now, 1))
        self.assertFalse(host_stamp_fresh(1_700_000_001, 0, now, 1))
        self.assertFalse(host_stamp_fresh(0, 0, now, 1))
        self.assertFalse(host_stamp_fresh(1_700_000_000, 1_000_000_000, now, 1))
        self.assertFalse(host_stamp_fresh(True, 0, now, 1))


if __name__ == '__main__':
    unittest.main()
