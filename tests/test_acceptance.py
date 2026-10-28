"""Synthetic fixtures only; these do not claim physical camera acceptance."""
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from runtime import acceptance as a


def stream(encoding='16UC1'):
    result = {'topic': '/synthetic_sensor/depth/image_raw', 'encoding': encoding,
              'width': 2, 'height': 2, 'frame_id': 'synthetic_depth_optical_frame',
              'required_reliability': 'reliable', 'min_frames': 2,
              'max_offset_seconds': 0.1, 'max_gap_seconds': 0.1, 'freshness_seconds': 0.2,
              'time': {'source': 'device', 'common_clock': 'unix', 'offset_ns': 1_000_000_000,
                       'mapping_evidence': 'measured'}}
    if encoding == '16UC1':
        result.update(depth_unit='mm', depth_alignment='raw', depth_reference={
            'method': 'known_distance', 'x': 1, 'y': 1, 'distance_mm': 1000, 'tolerance_mm': 10})
    return result


def message(big=False, value=1000):
    # Two pixels plus two padding bytes per row; reference is in second row.
    data = struct.pack('>HHH' if big else '<HHH', 123, value, 0) * 2
    return NS(width=2, height=2, step=6, data=data, encoding='16UC1', is_bigendian=int(big),
              header=NS(frame_id='synthetic_depth_optical_frame', stamp=NS(sec=1, nanosec=0)))


class AcceptanceTests(unittest.TestCase):
    def test_spec_requires_scale_evidence_and_time_mapping(self):
        a.validate_spec({'version': 1, 'streams': [stream()]})
        for key in ('depth_reference', 'time', 'required_reliability'):
            bad = stream(); bad.pop(key)
            with self.assertRaises(a.AcceptanceError): a.validate_spec({'version': 1, 'streams': [bad]})
        for value in (float('nan'), float('inf'), 0, -1):
            bad = stream(); bad['max_gap_seconds'] = value
            with self.assertRaises(a.AcceptanceError): a.validate_spec({'version': 1, 'streams': [bad]})

    def test_depth_endianness_and_padding(self):
        for big in (False, True):
            self.assertEqual(a.image_sample(message(big), stream(), 2_000_000_000)['reference_depth'], 1000)

    def test_actual_encoding_layout_and_timestamp_rejected(self):
        for field, value in [('encoding', 'rgb8'), ('step', 5), ('data', b'123'), ('is_bigendian', 2)]:
            msg = message(); setattr(msg, field, value)
            with self.assertRaises(a.AcceptanceError): a.image_sample(msg, stream(), 2_000_000_000)
        msg = message(); msg.header.stamp.nanosec = 1_000_000_000
        with self.assertRaises(a.AcceptanceError): a.image_sample(msg, stream(), 2_000_000_000)

    def test_qos_no_publisher_unknown_or_best_effort_offer_fails_reliable(self):
        for offers in ([], [{'reliability': 'best_effort', 'durability': 'volatile'}],
                       [{'reliability': 'unknown', 'durability': 'volatile'}]):
            self.assertFalse(a.qos_compatible(offers, 'reliable'))
        self.assertTrue(a.qos_compatible([{'reliability': 'reliable', 'durability': 'volatile'}], 'best_effort'))

    def report(self, value=1000, mutate=None, now=2_040_000_000):
        msg = message(value=value)
        samples = [a.image_sample(msg, stream(), 2_010_000_000)]
        msg.header.stamp.nanosec = 30_000_000
        samples.append(a.image_sample(msg, stream(), 2_040_000_000))
        if mutate: mutate(samples)
        return a.evaluate(stream(), samples, [{'reliability': 'reliable', 'durability': 'volatile'}], now)

    def test_controlled_reference_pass_and_claim_boundaries(self):
        report = self.report()
        self.assertTrue(report['ok'])
        self.assertFalse(report['hardware_exposure_sync_verified'])
        self.assertFalse(report['depth_alignment_verified'])

    def test_zero_depth_and_wrong_scale_fail(self):
        self.assertIn('DEPTH_REFERENCE_INVALID_SAMPLES', self.report(value=0)['errors'])
        self.assertIn('DEPTH_SCALE_REFERENCE_MISMATCH', self.report(value=1)['errors'])

    def test_clock_jump_gap_and_stale_rejected(self):
        self.assertIn('STAMP_NOT_ADVANCING', self.report(mutate=lambda s: s[1].update(stamp_ns=s[0]['stamp_ns']))['errors'])
        self.assertIn('STAMP_GAP', self.report(mutate=lambda s: s[1].update(stamp_ns=1_500_000_000))['errors'])
        self.assertIn('TIME_BASE_OFFSET', self.report(mutate=lambda s: s[0].update(receive_ns=4_000_000_000))['errors'])
        self.assertIn('SOURCE_STALE', self.report(now=3_000_000_000)['errors'])
        self.assertIn('RECEIVE_STALE', self.report(now=3_000_000_000)['errors'])

    def test_cli_validation_has_no_paths_topics_or_claim_of_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private-spec.json'
            path.write_text(json.dumps({'version': 1, 'streams': [stream()]}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output): code = a.main(['--spec', str(path), '--validate-only'])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output.getvalue())['hardware_verified'])
            self.assertNotIn('synthetic_sensor', output.getvalue())
            self.assertNotIn(folder, output.getvalue())

    def test_invalid_inputs_and_duplicate_publishers_fail_closed(self):
        for streams in ([None], [{'topic': []}], [stream(), stream()]):
            with self.assertRaises(a.AcceptanceError): a.validate_spec({'version': 1, 'streams': streams})
        offers = [{'reliability': 'reliable', 'durability': 'volatile'}] * 2
        self.assertFalse(a.qos_compatible(offers, 'reliable'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private.json'; path.write_text('INVALID PRIVATE CONTENT')
            output = io.StringIO()
            with contextlib.redirect_stdout(output): code = a.main(['--spec', str(path)])
            self.assertEqual(code, 1)
            self.assertNotIn(folder, output.getvalue())
            self.assertNotIn('PRIVATE', output.getvalue())

    def test_live_uses_required_qos_and_actual_endpoint_observation(self):
        import sys
        from types import ModuleType
        clock = [0.0]; callbacks = []; qos_used = []
        class Policy:
            RELIABLE = 1; BEST_EFFORT = 2; VOLATILE = 3; KEEP_LAST = 4
        class Node:
            def create_subscription(self, typ, topic, callback, qos):
                callbacks.append(callback); qos_used.append(qos); return callback
            def get_publishers_info_by_topic(self, topic):
                return [NS(qos_profile=NS(reliability=Policy.RELIABLE, durability=Policy.VOLATILE))]
            def destroy_node(self): pass
        def spin(node, timeout_sec):
            clock[0] += 0.01
            for callback in callbacks:
                msg = message(); msg.header.stamp.nanosec = int(clock[0] * 1e9)
                callback(msg)
        ros = ModuleType('rclpy'); ros.init = lambda: None; ros.shutdown = lambda: None
        ros.create_node = lambda name: Node(); ros.spin_once = spin
        qos = ModuleType('rclpy.qos'); qos.QoSProfile = lambda **kw: NS(**kw)
        for key in ('ReliabilityPolicy', 'DurabilityPolicy', 'HistoryPolicy'): setattr(qos, key, Policy)
        sensor = ModuleType('sensor_msgs'); msgs = ModuleType('sensor_msgs.msg'); msgs.Image = object
        with patch.dict(sys.modules, {'rclpy': ros, 'rclpy.qos': qos, 'sensor_msgs': sensor, 'sensor_msgs.msg': msgs}), \
             patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(a.time, 'time_ns', side_effect=lambda: 2_000_000_000 + int(clock[0] * 1e9)):
            report = a.run_live({'streams': [stream()]}, .05, .01)
        self.assertTrue(report['ok'], report)
        self.assertEqual(qos_used[0].reliability, Policy.RELIABLE)


if __name__ == '__main__': unittest.main()
