"""Synthetic calibration evidence only; no camera identity or physical claims."""
import legacy_support  # Enable the test-only Python behavior oracle.
import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from runtime import calibration_export

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import calibration
import configuration as cfg


def fixture():
    data = json.loads((BASE / 'inventory.v2.example.json').read_text())
    for index, camera in enumerate(data['cameras']):
        camera.update(enabled=True, serial_number='SYNTHETIC_' + str(index),
                      timestamp_source='synthetic-clock', calibration_revision='synthetic-calibration')
        camera['parameters'].update(color_width=4, color_height=2, color_fps=3,
                                    enable_depth=True, depth_width=2, depth_height=2, depth_fps=3)
        camera['health'] = dict(discovery_seconds=1, sample_seconds=2, freshness_seconds=1, min_fps_ratio=0.5)
    return data


def evidence(request):
    cameras = {}
    for camera in request['cameras']:
        streams = camera['streams']
        item = {}
        for stream, expected in streams.items():
            item[stream] = dict(expected, distortion_model='plumb_bob', d=[0.0]*5,
                                k=[2.,0.,1.,0.,2.,1.,0.,0.,1.], r=[1.,0.,0.,0.,1.,0.,0.,0.,1.],
                                p=[2.,0.,1.,0.,0.,2.,1.,0.,0.,0.,1.,0.])
        if set(streams) == {'color', 'depth'}:
            item['depth_to_color'] = dict(source_frame=streams['depth']['frame_id'],
                                         target_frame=streams['color']['frame_id'], translation_unit='m',
                                         rotation_representation='quaternion_xyzw',
                                         translation=[0.01,0.,0.], rotation=[0.,0.,0.,1.])
        cameras[camera['name']] = item
    return dict(source='offline-evidence', request_sha256=calibration.request_digest(request), cameras=cameras)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.generation = 'a'*32
        self.request = calibration.capture_request(self.data, self.generation)
        self.observed = evidence(self.request)
        self.first = next(iter(self.observed['cameras'].values()))

    def build(self):
        return calibration.build_snapshot(self.data, self.generation, self.observed)

    def rejects(self, code):
        with self.assertRaises(cfg.ConfigurationError) as raised:
            self.build()
        self.assertEqual(raised.exception.code, code)

    def test_snapshot_roundtrip_private_mode_and_allowlisted_summary(self):
        snapshot = self.build()
        self.assertFalse(snapshot['physical_accuracy_verified'])
        self.assertFalse(snapshot['robot_hand_eye_included'])
        self.assertEqual(calibration.validate_snapshot(snapshot, self.data, self.generation), snapshot)
        with tempfile.TemporaryDirectory() as root:
            summary = calibration.save_snapshot(root, snapshot, self.data, self.generation)
            path = next((Path(root) / 'state/calibration').glob('*.json'))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), snapshot)
            self.assertEqual(set(summary), {'kind', 'camera_count', 'structural_validation',
                                           'physical_accuracy_verified', 'snapshot_sha256'})

    def test_missing_camera_and_missing_transform_rejected(self):
        self.first.pop('depth_to_color')
        self.rejects('CALIBRATION_MISSING')
        self.observed = evidence(self.request)
        self.observed['cameras'].pop(next(iter(self.observed['cameras'])))
        self.rejects('CALIBRATION_MISSING')

    def test_wrong_geometry_and_frame_rejected(self):
        self.first['color']['width'] += 2
        self.rejects('CALIBRATION_BINDING')
        self.first['color']['width'] -= 2
        self.first['color']['frame_id'] = 'synthetic_other_optical'
        self.rejects('CALIBRATION_BINDING')

    def test_zero_focal_nonfinite_and_cropped_intrinsics_rejected(self):
        self.first['color']['k'][0] = 0
        self.rejects('CALIBRATION_UNCALIBRATED')
        self.first['color']['k'][0] = float('nan')
        self.rejects('CALIBRATION_INVALID')
        self.first['color']['k'][0] = 2.
        self.first['color']['roi'] = {'width': 1}
        self.rejects('CALIBRATION_GEOMETRY')

    def test_direction_unit_and_quaternion_rejected(self):
        ex = self.first['depth_to_color']
        ex['translation_unit'] = 'mm'
        self.rejects('CALIBRATION_TRANSFORM')
        ex['translation_unit'] = 'm'
        ex['source_frame'], ex['target_frame'] = ex['target_frame'], ex['source_frame']
        self.rejects('CALIBRATION_TRANSFORM')
        ex['source_frame'], ex['target_frame'] = ex['target_frame'], ex['source_frame']
        ex['rotation'] = [0.,0.,0.,2.]
        self.rejects('CALIBRATION_TRANSFORM')

    def test_generation_profile_and_revision_drift_rejected(self):
        snapshot = self.build()
        for field in ('serial_number', 'calibration_revision'):
            changed = copy.deepcopy(self.data)
            changed['cameras'][0][field] = 'synthetic-changed'
            with self.assertRaises(cfg.ConfigurationError):
                calibration.validate_snapshot(snapshot, changed, self.generation)
        with self.assertRaises(cfg.ConfigurationError):
            calibration.validate_snapshot(snapshot, self.data, 'b'*32)

    def test_rehashed_invalid_snapshot_still_rejected(self):
        snapshot = self.build()
        snapshot['cameras'][0]['intrinsics']['color']['k'][0] = 0
        snapshot['snapshot_sha256'] = calibration.request_digest({k:v for k,v in snapshot.items() if k != 'snapshot_sha256'})
        with self.assertRaises(cfg.ConfigurationError):
            calibration.validate_snapshot(snapshot, self.data, self.generation)

    def test_single_stream_does_not_invent_d2c(self):
        for camera in self.data['cameras']:
            camera['parameters']['enable_depth'] = False
        self.request = calibration.capture_request(self.data, self.generation)
        self.observed = evidence(self.request)
        self.assertTrue(all('depth_to_color' not in c for c in self.build()['cameras']))

    def test_malformed_evidence_has_stable_redacted_error(self):
        self.first['color']['roi'] = 'synthetic-private-value'
        with self.assertRaises(cfg.ConfigurationError) as raised:
            self.build()
        self.assertEqual(raised.exception.code, 'CALIBRATION_INVALID')
        self.assertNotIn('synthetic-private-value', str(raised.exception))

    def test_ros_collector_uses_published_static_tf_and_camerainfo(self):
        # Exercise graph direction and late-joining static TF delivery with fake ROS modules.
        callbacks = []
        node = MagicMock()
        node.create_subscription.side_effect = lambda typ, topic, cb, qos: callbacks.append((topic, cb)) or object()
        ros = MagicMock()
        ros.create_node.return_value = node
        tf_buffer = MagicMock()
        tf_buffer.lookup_transform.return_value = SimpleNamespace(transform=SimpleNamespace(
            translation=SimpleNamespace(x=.01, y=0., z=0.),
            rotation=SimpleNamespace(x=0., y=0., z=0., w=1.)))
        tf = MagicMock()
        tf.Buffer.return_value = tf_buffer
        tf.TransformException = LookupError
        by_topic = {expected['topic']: info for camera in self.request['cameras']
                    for stream, expected in camera['streams'].items()
                    for info in [self.observed['cameras'][camera['name']][stream]]}
        def spin_once(*args, **kwargs):
            for topic, callback in callbacks:
                if topic == '/tf_static':
                    callback(SimpleNamespace(transforms=[object()]))
                else:
                    info = by_topic[topic]
                    callback(SimpleNamespace(header=SimpleNamespace(frame_id=info['frame_id']),
                        width=info['width'], height=info['height'], distortion_model=info['distortion_model'],
                        k=info['k'], d=info['d'], r=info['r'], p=info['p'], binning_x=0, binning_y=0,
                        roi=SimpleNamespace(x_offset=0, y_offset=0, width=0, height=0)))
        ros.spin_once.side_effect = spin_once
        with patch.dict(sys.modules, {'rclpy': ros, 'rclpy.qos': MagicMock(), 'rclpy.time': MagicMock(),
                                      'sensor_msgs': MagicMock(), 'sensor_msgs.msg': MagicMock(),
                                      'tf2_ros': tf, 'tf2_msgs': MagicMock(), 'tf2_msgs.msg': MagicMock()}):
            result = calibration_export.collect(self.request, timeout_seconds=1)
        self.assertEqual(result['source'], 'ros-camera-info-tf')
        snapshot = calibration.build_snapshot(self.data, self.generation, result)
        self.assertEqual(len(snapshot['cameras']), len(self.request['cameras']))
        self.assertNotIn('/tf', [topic for topic, callback in callbacks])
        tf_buffer.set_transform_static.assert_called()
        for camera in self.request['cameras']:
            tf_buffer.lookup_transform.assert_any_call(camera['streams']['color']['frame_id'],
                                                       camera['streams']['depth']['frame_id'], unittest.mock.ANY)
        node.destroy_node.assert_called_once()
        ros.shutdown.assert_called_once()

    def test_distortion_models_require_exact_coefficient_counts(self):
        for model, count in (('plumb_bob', 5), ('rational_polynomial', 8), ('equidistant', 4)):
            with self.subTest(model=model):
                self.first['color'].update(distortion_model=model, d=[0.] * count)
                self.build()
                for invalid_count in (0, count - 1, count + 1):
                    self.first['color']['d'] = [0.] * invalid_count
                    self.rejects('CALIBRATION_DISTORTION')
        self.first['color'].update(distortion_model='synthetic-unknown-model', d=[0.] * 5)
        self.rejects('CALIBRATION_DISTORTION')

    def test_intrinsics_bottom_row_must_be_homogeneous(self):
        for index, value in ((6, .1), (7, .1), (8, 2.)):
            with self.subTest(index=index):
                self.first['color']['k'] = [2.,0.,1.,0.,2.,1.,0.,0.,1.]
                self.first['color']['k'][index] = value
                self.rejects('CALIBRATION_UNCALIBRATED')

    def test_invalid_rectification_is_rejected(self):
        self.first['color']['r'] = [0.] * 9
        self.rejects('CALIBRATION_GEOMETRY')


if __name__ == '__main__':
    unittest.main()
