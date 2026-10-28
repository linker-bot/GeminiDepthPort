"""All startup surfaces delegate to the same non-destructive lifecycle path."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment_control as deployment


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.patches = [patch.object(deployment.ctl, 'ROOT', self.root),
                        patch.object(deployment.ctl, 'load', return_value={'schema_version': 1}),
                        patch.object(deployment.ctl, 'digest', return_value='synthetic-revision')]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def test_unconfigured_boot_keeps_management_without_opening_cameras(self):
        with patch.object(deployment.ctl, 'validate', side_effect=deployment.ctl.ConfigurationError('UNBOUND','unbound')), \
             patch.object(deployment, 'Manager') as manager, patch.object(deployment.ctl, 'ensure_started') as start:
            self.assertEqual(deployment.start('boot'), 0)
            manager.assert_not_called(); start.assert_not_called()

    def test_manual_start_rejects_unconfigured_inventory(self):
        with patch.object(deployment.ctl, 'validate', side_effect=deployment.ctl.ConfigurationError('UNBOUND','unbound')):
            with self.assertRaises(deployment.ctl.ConfigurationError): deployment.start('up')

    def test_boot_never_stops_previews_or_cameras_before_ensure(self):
        manager = Mock()
        with patch.object(deployment.ctl, 'validate'), patch.object(deployment, 'Manager', return_value=manager), \
             patch.object(deployment.ctl, 'ensure_started') as start, patch.object(deployment.ctl, 'stop_cameras') as stop:
            self.assertEqual(deployment.start('boot'), 0)
            start.assert_called_once(); stop.assert_not_called(); manager.stop_previews.assert_not_called()
            self.assertEqual(manager.persist_job.call_args.args[0]['status'], 'passed')

    def test_failed_boot_is_recorded_without_raw_error_output(self):
        manager = Mock()
        with patch.object(deployment.ctl, 'validate'), patch.object(deployment, 'Manager', return_value=manager), \
             patch.object(deployment.ctl, 'ensure_started', side_effect=RuntimeError('PRIVATE_SENTINEL')):
            with self.assertRaises(RuntimeError): deployment.start('boot')
            job = manager.persist_job.call_args.args[0]
            self.assertEqual(job['status'], 'failed')
            self.assertNotIn('PRIVATE_SENTINEL', str(job))
