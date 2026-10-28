"""Probe logic tests with a virtual clock and synthetic ROS messages, not hardware tests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]

class HealthTests(unittest.TestCase):
    def probe(self, mode, strict=False, qos_required=False, host=False):
        self_test = self
        now = [0.0]
        callbacks = []
        class Node:
            def create_subscription(self, typ, topic, callback, qos):
                if qos_required:
                    self_test.assertEqual(qos.reliability, 'RELIABLE')
                    self_test.assertEqual(qos.durability, 'VOLATILE')
                    self_test.assertEqual(qos.history, 'KEEP_LAST')
                    self_test.assertEqual(qos.depth, 10)
                else:
                    self_test.assertIs(qos, sensor_qos)
                if mode != 'best_effort' or not qos_required:
                    callbacks.append((topic, callback))
                return callback
            def get_publishers_info_by_topic(self, topic):
                if mode == 'no_publisher': return []
                reliability = 'best_effort' if mode == 'best_effort' else 'reliable'
                if mode == 'info_best_effort' and topic.endswith('camera_info'):
                    reliability = 'best_effort'
                return [SimpleNamespace(qos_profile={'reliability': reliability, 'durability': 'volatile'})]
            def destroy_node(self): pass
        def spin_once(node, timeout_sec):
            now[0] += timeout_sec
            for topic, callback in callbacks:
                if mode == 'missing' and 'depth' in topic: continue
                if mode == 'slow' and round(now[0] * 10) % 4: continue
                sec = 1 if mode == 'frozen' else int(now[0]) + 1
                nano = 0 if mode == 'frozen' else int((now[0] % 1) * 1e9)
                if host:
                    sec = 1_700_000_000 + int(now[0])
                    if mode == 'stale_source': sec -= 10
                    if mode == 'future_source': sec += 1
                msg = SimpleNamespace(width=320 if mode == 'wrong_size' else 640, height=480,
                    header=SimpleNamespace(frame_id='head_color_optical_frame',
                                           stamp=SimpleNamespace(sec=sec, nanosec=nano)))
                if strict:
                    msg.encoding=('yuyv' if 'color' in topic else '16UC1')
                    msg.step=msg.width*2;msg.data=bytes(msg.height*msg.step)
                    msg.header.frame_id='synthetic_'+('color' if 'color' in topic else 'depth')+'_optical_frame'
                    if mode=='bad_encoding':msg.encoding='rgb8'
                    if mode=='bad_step':msg.step+=1
                    if mode=='truncated':msg.data=msg.data[:-1]
                    if mode=='bad_frame':msg.header.frame_id='wrong'
                callback(msg)
        ros = ModuleType('rclpy'); ros.init = lambda: None; ros.shutdown = lambda: None
        ros.create_node = lambda name: Node(); ros.spin_once = spin_once
        qos = ModuleType('rclpy.qos'); sensor_qos = object(); qos.qos_profile_sensor_data = sensor_qos
        qos.QoSProfile = lambda **kw: SimpleNamespace(**kw)
        qos.ReliabilityPolicy = SimpleNamespace(RELIABLE='RELIABLE')
        qos.DurabilityPolicy = SimpleNamespace(VOLATILE='VOLATILE')
        qos.HistoryPolicy = SimpleNamespace(KEEP_LAST='KEEP_LAST')
        sensor = ModuleType('sensor_msgs'); msgs = ModuleType('sensor_msgs.msg'); msgs.Image = object
        modules = {'rclpy': ros, 'rclpy.qos': qos, 'sensor_msgs': sensor, 'sensor_msgs.msg': msgs}
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location('probe', BASE / 'runtime/health.py')
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        config = {'camera_name': 'head', 'discovery_seconds': 1, 'sample_seconds': 2,
                  'freshness_seconds': 1, 'streams': [
                      {'topic': '/head/' + s + '/image_raw', 'width': 640, 'height': 480, 'min_fps': 9}
                      for s in ('color', 'depth')]}
        if qos_required:
            for stream in config['streams']:
                stream.update(required_reliability='reliable', camera_info_topic=stream['topic'].replace('image_raw', 'camera_info'))
        if strict:
            config['camera_name']='synthetic'
            for stream,spec in zip(('color','depth'),config['streams']):
                spec.update(encoding='yuyv' if stream=='color' else '16UC1',frame_id='synthetic_'+stream+'_optical_frame')
        with patch.object(module.Path, 'read_text', return_value=json.dumps(config)), \
             patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(module.time, 'time_ns', side_effect=lambda: 1_700_000_000_000_000_000 + int(now[0] * 1e9)), \
             patch.dict(os.environ, {'GDP_VALIDATE_HOST_STAMPS': '1' if host else '0'}), \
             contextlib.redirect_stdout(io.StringIO()):
            return module.main()
    def test_explicit_host_clock_rejects_stale_or_future_source_despite_fresh_receives(self):
        self.assertEqual(self.probe('good', strict=True, host=True), 0)
        for mode in ('stale_source', 'future_source'):
            with self.subTest(mode=mode): self.assertEqual(self.probe(mode, strict=True, host=True), 1)

    def test_actual_image_and_camera_info_offers_must_satisfy_recorder(self):
        self.assertEqual(self.probe('good', strict=True, qos_required=True), 0)
        for mode in ('no_publisher', 'best_effort', 'info_best_effort'):
            with self.subTest(mode=mode):
                self.assertEqual(self.probe(mode, strict=True, qos_required=True), 1)

    def test_strict_contract_accepts_native_layout(self):self.assertEqual(self.probe('good',strict=True),0)
    def test_strict_contract_rejects_downgrade_and_malformed_data(self):
        for mode in ('bad_encoding','bad_step','truncated','bad_frame','frozen'):
            with self.subTest(mode=mode):self.assertEqual(self.probe(mode,strict=True),1)
    def test_both_streams_advance(self): self.assertEqual(self.probe('good'), 0)
    def test_missing_stream(self): self.assertEqual(self.probe('missing'), 1)
    def test_frozen_stamp(self): self.assertEqual(self.probe('frozen'), 1)
    def test_wrong_dimensions(self): self.assertEqual(self.probe('wrong_size'), 1)
    def test_insufficient_fps(self): self.assertEqual(self.probe('slow'), 1)

if __name__ == '__main__': unittest.main()
