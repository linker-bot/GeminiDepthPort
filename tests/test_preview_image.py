"""Decode ROS image buffers without assuming packed rows. Encoder requires real OpenCV."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import patch
import numpy as np

BASE=Path(__file__).resolve().parents[1]
class PreviewBufferTests(unittest.TestCase):
    def setUp(self):
        modules={name:ModuleType(name) for name in ('cv2','rclpy','rclpy.qos','sensor_msgs','sensor_msgs.msg')}
        modules['rclpy.qos'].qos_profile_sensor_data=object()
        modules['sensor_msgs.msg'].Image=object
        with patch.dict(sys.modules,modules):
            spec=importlib.util.spec_from_file_location('bridge',BASE/'runtime/preview_bridge.py')
            self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
    def test_depth_stride_and_big_endian(self):
        msg=SimpleNamespace(encoding='16UC1',width=2,height=2,step=6,is_bigendian=True,
                            data=bytes([0,100,0,200,0,0,1,44,1,144,0,0]))
        image,encoding=self.module.image_array(msg)
        np.testing.assert_array_equal(image,[[100,200],[300,400]])
    def test_rgb_stride_ignores_row_padding(self):
        msg=SimpleNamespace(encoding='rgb8',width=1,height=2,step=4,is_bigendian=False,data=bytes([1,2,3,0,4,5,6,0]))
        image,encoding=self.module.image_array(msg)
        np.testing.assert_array_equal(image,[[[1,2,3]],[[4,5,6]]])
    def test_float_depth(self):
        msg=SimpleNamespace(encoding='32FC1',width=2,height=1,step=8,is_bigendian=False,
                            data=np.array([0.5,1.0],dtype='<f4').tobytes())
        image,encoding=self.module.image_array(msg)
        np.testing.assert_array_equal(image,[[0.5,1.0]])
    def test_yuyv_owned_layout_and_unchanged_source(self):
        source=bytearray([16,128,32,128,64,90,80,240])
        msg=SimpleNamespace(encoding='yuyv',width=2,height=2,step=4,is_bigendian=True,data=source)
        image,encoding=self.module.image_array(msg)
        self.assertEqual(image.shape,(2,2,2));self.assertEqual(encoding,'yuyv')
        self.assertEqual(image.tobytes(),bytes(source))
        image[:]=0
        self.assertEqual(bytes(source),bytes([16,128,32,128,64,90,80,240]))
    def test_yuyv_rejects_odd_width_padding_and_wrong_payload(self):
        for width,step,payload in [(3,6,bytes(12)),(2,6,bytes(12)),(2,4,bytes(7)),(2,4,bytes(9))]:
            msg=SimpleNamespace(encoding='yuyv',width=width,height=2,step=step,data=payload)
            with self.assertRaises(ValueError):self.module.image_array(msg)
    def test_unknown_encoding_rejected(self):
        with self.assertRaises(ValueError): self.module.image_array(SimpleNamespace(encoding='yuv422'))


class RealOpenCVYUYVTests(unittest.TestCase):
    def test_real_jpeg_preserves_black_white_and_does_not_mutate_source(self):
        try:import cv2
        except ImportError:self.skipTest('OpenCV is tested in the driver image job')
        modules={name:ModuleType(name) for name in ('rclpy','rclpy.qos','sensor_msgs','sensor_msgs.msg')}
        modules['rclpy.qos'].qos_profile_sensor_data=object();modules['sensor_msgs.msg'].Image=object
        with patch.dict(sys.modules,modules):
            spec=importlib.util.spec_from_file_location('real_preview',BASE/'runtime/preview_bridge.py')
            bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
        original=bytearray([16,128,235,128])
        msg=SimpleNamespace(encoding='yuyv',width=2,height=1,step=4,is_bigendian=False,data=original)
        encoded=bridge.jpeg_image(msg,'color')
        decoded=cv2.imdecode(np.frombuffer(encoded,dtype=np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape,(1,2,3))
        self.assertLess(int(decoded[0,0].max()),15)
        self.assertGreater(int(decoded[0,1].min()),240)
        self.assertEqual(original,bytearray([16,128,235,128]))
