"""Probe all enabled image streams; exit 0 only when all are fresh and advancing."""
import json
import os
import time
import uuid
from pathlib import Path
import rclpy
from rclpy.qos import (qos_profile_sensor_data, QoSProfile, ReliabilityPolicy,
                       DurabilityPolicy, HistoryPolicy)
from sensor_msgs.msg import Image
try:
    from .source_time import host_stamp_fresh
except ImportError:
    try:
        from source_time import host_stamp_fresh
    except ImportError:
        from runtime.source_time import host_stamp_fresh
try:
    from .qos_contract import validate_offered_qos
except ImportError:
    try:
        from qos_contract import validate_offered_qos
    except ImportError:
        from runtime.qos_contract import validate_offered_qos

def subscription_qos(spec):
    # Verify the actual declared recording reliability with the subscriber too.
    # Best-effort can lose fragmented native YUYV frames on UDP loopback even
    # while the same publisher sustains the required reliable consumer rate.
    if spec.get('required_reliability') == 'reliable':
        return QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10,
                          reliability=ReliabilityPolicy.RELIABLE,
                          durability=DurabilityPolicy.VOLATILE)
    return qos_profile_sensor_data


def main():
    cfg = json.loads(Path('/config/health.json').read_text())
    require_host_stamps = os.environ.get('GDP_VALIDATE_HOST_STAMPS') == '1'
    rclpy.init()
    node = rclpy.create_node('camera_probe_' + uuid.uuid4().hex[:10])
    stats = {s['topic']: {'count': 0, 'last': 0, 'stamps': set(), 'last_stamp': None, 'bad': False} for s in cfg['streams']}
    sampling = [False]
    subscriptions = []
    def make_callback(spec):
        def callback(msg):
            if not sampling[0]:
                return
            st = stats[spec['topic']]
            st['count'] += 1
            st['last'] = time.monotonic()
            st['stamps'].add((msg.header.stamp.sec, msg.header.stamp.nanosec))
            stamp = (msg.header.stamp.sec, msg.header.stamp.nanosec)
            if require_host_stamps:
                st['bad'] |= not host_stamp_fresh(*stamp, time.time_ns(), cfg['freshness_seconds'])
            if 'encoding' in spec:
                st['bad'] |= (getattr(msg, 'encoding', None) != spec['encoding']
                              or msg.header.frame_id != spec['frame_id']
                              or msg.width <= 0 or msg.height <= 0
                              or getattr(msg, 'step', 0) < msg.width * 2
                              or len(getattr(msg, 'data', b'')) != msg.height * getattr(msg, 'step', 0)
                              or (spec['encoding'] == 'yuyv' and
                                  (msg.width % 2 != 0 or msg.step != msg.width * 2))
                              or (spec['encoding'] == '16UC1' and msg.step % 2 != 0)
                              or (st['last_stamp'] is not None and stamp <= st['last_stamp']))
            st['last_stamp'] = stamp
            st['bad'] |= (msg.width != spec['width'] or msg.height != spec['height']
                          or not msg.header.frame_id.startswith(cfg['camera_name'] + '_')
                          or msg.header.stamp.sec == 0 and msg.header.stamp.nanosec == 0)
        return callback
    try:
        for spec in cfg['streams']:
            subscriptions.append(node.create_subscription(Image, spec['topic'], make_callback(spec), subscription_qos(spec)))
        end = time.monotonic() + cfg['discovery_seconds']
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
        sampling[0] = True
        start = time.monotonic()
        end = start + cfg['sample_seconds']
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
        now = time.monotonic()
        result = []
        for spec in cfg['streams']:
            st = stats[spec['topic']]
            fps = st['count'] / (now - start)
            ok = (fps >= spec['min_fps'] and now - st['last'] <= cfg['freshness_seconds']
                  and len(st['stamps']) >= 2 and not st['bad'])
            if require_host_stamps:
                ok = ok and st['last_stamp'] is not None and host_stamp_fresh(
                    *st['last_stamp'], time.time_ns(), cfg['freshness_seconds'])
            qos_results = []
            if 'required_reliability' in spec:
                for topic in (spec['topic'], spec.get('camera_info_topic')):
                    if topic is None:
                        continue
                    offers = [endpoint.qos_profile for endpoint in node.get_publishers_info_by_topic(topic)]
                    qos_results.append(validate_offered_qos(offers, spec['required_reliability']))
                ok = ok and all(item['ready'] for item in qos_results)
            result.append({'topic': spec['topic'], 'fps': round(fps, 2), 'minimum': spec['min_fps'],
                           'qos': qos_results, 'ok': ok})
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result and all(x['ok'] for x in result) else 1
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    raise SystemExit(main())
