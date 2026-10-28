"""Control-plane capture uses only an existing ROS container and private evidence."""
import legacy_support  # Enable the test-only Python behavior oracle.
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import cameractl as ctl


class CalibrationControlTests(unittest.TestCase):
    def test_export_checks_active_group_and_uses_stdin(self):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = {'generation':'a'*32, 'cameras':[{'name':'camera_a','service':'camera_camera_a'}]}
            existing = {'camera_a':{'Id':'synthetic-container'}}
            request = {'kind':'synthetic-request'}
            with patch.object(ctl,'ROOT',Path(folder)), patch.object(ctl,'load',return_value={'synthetic':True}), \
                 patch.object(ctl,'check_render',return_value=snapshot), \
                 patch.object(ctl.lifecycle,'check_existing',return_value=existing), \
                 patch.object(ctl.lifecycle,'probe') as probe, \
                 patch('calibration.capture_request',return_value=request), \
                 patch('calibration.build_snapshot',return_value={'synthetic':True}) as build, \
                 patch('calibration.save_snapshot',return_value={'camera_count':1}) as save, \
                 patch.object(ctl.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as collect, \
                 patch.object(ctl,'run',side_effect=['{"source":"ros-camera-info-tf"}','']) as command:
                self.assertEqual(ctl.export_calibration(),{'camera_count':1})
            args = collect.call_args.args[0]
            self.assertEqual(args[:4],['docker','exec','-i','synthetic-container'])
            self.assertIn('-',args)
            self.assertEqual(json.loads(collect.call_args.kwargs['input']),request)
            self.assertEqual(probe.call_count,1)
            self.assertEqual(build.call_args.args[2],{'source':'ros-camera-info-tf'})
            self.assertEqual(command.call_args.args[0][3:5],['rm','-f'])
            save.assert_called_once()

    def test_missing_group_fails_before_collecting(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(ctl,'ROOT',Path(folder)), patch.object(ctl,'load',return_value={}), \
                 patch.object(ctl,'check_render',return_value={'cameras':[{'name':'camera_a'}]}), \
                 patch.object(ctl.lifecycle,'check_existing',return_value={}), \
                 patch.object(ctl.subprocess,'run') as collect:
                with self.assertRaises(ctl.ConfigurationError): ctl.export_calibration()
                collect.assert_not_called()
