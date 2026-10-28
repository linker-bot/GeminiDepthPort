"""Read already-published CameraInfo and TF; never open a camera device.

The request and output are private files. This collector only gathers evidence;
calibration.build_snapshot must validate it against the active generation.
"""
import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
import tempfile
import time
import uuid


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def camera_info_record(msg):
    return {'frame_id': msg.header.frame_id, 'width': msg.width, 'height': msg.height,
            'distortion_model': msg.distortion_model, 'k': list(msg.k), 'd': list(msg.d),
            'r': list(msg.r), 'p': list(msg.p), 'binning_x': msg.binning_x, 'binning_y': msg.binning_y,
            'roi': {key: getattr(msg.roi, key) for key in ('x_offset', 'y_offset', 'height', 'width')}}


def collect(request, timeout_seconds=15):
    if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 120:
        raise ValueError('Invalid calibration collection timeout')
    # ROS is optional for offline contract tests and management-only installations.
    import rclpy
    from rclpy.qos import qos_profile_sensor_data, QoSProfile, DurabilityPolicy, ReliabilityPolicy
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo
    from tf2_ros import Buffer, TransformException
    from tf2_msgs.msg import TFMessage
    result = {'source': 'ros-camera-info-tf', 'request_sha256': _digest(request),
              'cameras': {c['name']: {} for c in request['cameras']}}
    rclpy.init()
    node = rclpy.create_node('calibration_reader_' + uuid.uuid4().hex[:10])
    buffer = Buffer()
    # D2C is a static factory transform. Never silently use moving robot TF.
    def static_tf(msg):
        for transform in msg.transforms:
            buffer.set_transform_static(transform, "published-static-tf")
    subscriptions = [node.create_subscription(
        TFMessage, "/tf_static", static_tf,
        QoSProfile(depth=100, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                   reliability=ReliabilityPolicy.RELIABLE))]
    try:
        for camera in request['cameras']:
            for stream, expected in camera['streams'].items():
                def callback(msg, name=camera['name'], stream=stream):
                    result['cameras'][name][stream] = camera_info_record(msg)
                subscriptions.append(node.create_subscription(CameraInfo, expected['topic'], callback, qos_profile_sensor_data))
        end = time.monotonic() + timeout_seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
            complete = True
            for camera in request['cameras']:
                streams, observed = camera['streams'], result['cameras'][camera['name']]
                if set(streams) == {'color', 'depth'}:
                    source, target = streams['depth']['frame_id'], streams['color']['frame_id']
                    try:
                        tf = buffer.lookup_transform(target, source, Time()).transform
                        observed['depth_to_color'] = {'source_frame': source, 'target_frame': target,
                            'translation_unit': 'm', 'rotation_representation': 'quaternion_xyzw',
                            'translation': [tf.translation.x, tf.translation.y, tf.translation.z],
                            'rotation': [tf.rotation.x, tf.rotation.y, tf.rotation.z, tf.rotation.w]}
                    except TransformException:
                        complete = False
                complete &= all(stream in observed for stream in streams)
            if complete:
                return result
        raise ValueError('Calibration evidence incomplete')
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main():
    parser = argparse.ArgumentParser(description='Collect private CameraInfo/TF evidence without opening devices')
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout', type=float, default=15)
    args = parser.parse_args()
    temporary = None
    try:
        request = json.load(sys.stdin) if args.request == '-' else json.loads(Path(args.request).read_text())
        evidence = collect(request, args.timeout)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.calibration-', dir=output.parent)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(evidence, handle, allow_nan=False)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, output)
        print(json.dumps({'collected': True, 'camera_count': len(request['cameras'])}))
        return 0
    except Exception:
        # Raw ROS exceptions can disclose topic bindings, frames, paths and host details.
        print(json.dumps({'collected': False, 'code': 'CALIBRATION_COLLECTION_FAILED'}))
        return 1
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    raise SystemExit(main())
