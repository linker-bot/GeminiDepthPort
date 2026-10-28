"""Synthetic ROS transport and OpenCV smoke test. Never creates a camera SDK context."""
import importlib.util
import os
from pathlib import Path
import time
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


def main():
    rclpy.init()
    node = rclpy.create_node('synthetic_yuyv_contract')
    received = []
    sub = node.create_subscription(Image, '/synthetic_yuyv/image_raw', received.append, qos_profile_sensor_data)
    publisher = node.create_publisher(Image, '/synthetic_yuyv/image_raw', qos_profile_sensor_data)
    expected = bytes([16,128,32,128,64,90,80,240])
    msg = Image(); msg.width = 2; msg.height = 2; msg.encoding = 'yuyv'; msg.step = 4
    msg.data = expected; msg.header.frame_id = 'synthetic_color_optical_frame'
    msg.header.stamp.sec = 7; msg.header.stamp.nanosec = 11
    try:
        deadline = time.monotonic() + 15
        while not received and time.monotonic() < deadline:
            publisher.publish(msg); rclpy.spin_once(node, timeout_sec=0.1)
        if not received:
            raise RuntimeError('Synthetic ROS image transport timed out')
        actual = received[0]
        if (actual.encoding != 'yuyv' or actual.step != 4 or bytes(actual.data) != expected
                or actual.header != msg.header or actual.width != 2 or actual.height != 2):
            raise RuntimeError('Synthetic ROS native payload or header changed')
        root = Path(os.environ.get('PREVIEW_RUNTIME_ROOT', '/app'))
        spec = importlib.util.spec_from_file_location('preview_bridge', root / 'preview_bridge.py')
        bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
        jpeg = bridge.jpeg_image(actual, 'color')
        import cv2
        import numpy as np
        pixels = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if pixels is None or pixels.shape != (2,2,3) or not jpeg.startswith(b'\xff\xd8'):
            raise RuntimeError('Synthetic OpenCV YUYV preview failed')
        if bytes(actual.data) != expected:
            raise RuntimeError('Preview mutated the original ROS payload')
        print('Synthetic ROS YUYV payload, header and real OpenCV preview passed; hardware not exercised.')
    finally:
        node.destroy_subscription(sub); node.destroy_node(); rclpy.shutdown()


if __name__ == '__main__': main()
