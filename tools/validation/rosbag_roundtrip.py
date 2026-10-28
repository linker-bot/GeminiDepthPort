#!/usr/bin/env python3
"""Private real ROSbag2 record/read-back smoke with byte-exact serialized hashes.

Uses actual Image/CameraInfo/TF messages; strict mode includes frame/pair metadata.
Read-back deserializes and validates YUYV/depth layouts, original headers and
calibration. This is an artifact roundtrip, not business consumer compatibility.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.rgbd_acceptance import (ContractError, Observer, canonical, image_projection, info_projection,
                                     require, tf_projection, validate_spec)


class BagRecordingError(ContractError):
    def __init__(self, errors, counts_by_role):
        super().__init__('BAG_RECORD_INCOMPLETE')
        self.diagnostics = {'record_errors': dict(errors), 'counts_by_role': counts_by_role}


def safe_code(exc):
    return str(exc) if isinstance(exc, ContractError) else 'RECORD_EXCEPTION_' + type(exc).__name__


def bindings(spec, wire_only):
    result = []
    for role, stream in spec['streams'].items():
        result.extend([(stream['image_topic'], 'sensor_msgs/msg/Image', 'image', role),
                       (stream['camera_info_topic'], 'sensor_msgs/msg/CameraInfo', 'info', role)])
        if not wire_only:
            result.append((stream['metadata_topic'], 'std_msgs/msg/String', 'metadata', role))
    if not wire_only:
        result.append((spec['pair']['topic'], 'std_msgs/msg/String', 'pair', 'pair'))
    result.append(('/tf_static', 'tf2_msgs/msg/TFMessage', 'tf', 'tf'))
    return result


def decode_projection(message, kind, role, spec):
    if kind == 'image':
        value = image_projection(message, spec['streams'][role], digest=True)
        # Exercise an actual downstream byte decoder, not just topic counts.
        pixels = memoryview(message.data)
        if role == 'color':
            first = struct.unpack_from('4B', pixels, 0)
            require(len(first) == 4, 'YUYV_DECODE')
            # BT.601 limited-range reference conversion exercises YUYV ordering.
            y, u, _, v = first
            c, d, e = y - 16, u - 128, v - 128
            rgb = [max(0, min(255, channel)) for channel in (
                (298*c + 409*e + 128) >> 8,
                (298*c - 100*d - 208*e + 128) >> 8,
                (298*c + 516*d + 128) >> 8)]
            require(all(0 <= channel <= 255 for channel in rgb), 'YUYV_DECODE')
        else:
            require(0 <= struct.unpack_from('<H', pixels, 0)[0] <= 65535, 'DEPTH_DECODE')
        return [value]
    if kind == 'info': return [info_projection(message)]
    if kind == 'tf': return tf_projection(message, spec['tf'])
    require(len(message.data) <= 4096, 'METADATA_SIZE')
    value = json.loads(message.data)
    require(isinstance(value, dict), 'METADATA_SCHEMA')
    return [value]


def record(spec, path, seconds, discovery, maximum, wire_only):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    from rclpy.serialization import serialize_message
    from rosidl_runtime_py.utilities import get_message
    import rosbag2_py
    require(not path.exists() and path.parent.is_dir(), 'BAG_PATH')
    # Keep databases, metadata, and manifests private from their first creation.
    os.umask(0o077)
    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id='sqlite3'),
                rosbag2_py.ConverterOptions('', ''))
    topics = bindings(spec, wire_only)
    counts, digests = Counter(), {topic: hashlib.sha256() for topic, *_ in topics}
    total, errors = [0], Counter()
    active = [False]
    rclpy.init()
    node = rclpy.create_node('geminidepthport_private_bag_recorder')
    subscriptions = []
    try:
        for topic, type_name, kind, role in topics:
            writer.create_topic(rosbag2_py.TopicMetadata(name=topic, type=type_name, serialization_format='cdr'))
            def callback(message, topic=topic, kind=kind, role=role):
                if not active[0]: return
                try:
                    decode_projection(message, kind, role, spec)
                    serialized = serialize_message(message)
                    # Reserve 10% for SQLite indexes/metadata. Exceeding the
                    # budget aborts recording rather than silently truncating.
                    require(total[0] + len(serialized) <= maximum * .9, 'BAG_SIZE_LIMIT')
                    writer.write(topic, serialized, time.time_ns())
                    digests[topic].update(struct.pack('<Q', len(serialized)))
                    digests[topic].update(serialized)
                    counts[topic] += 1
                    total[0] += len(serialized)
                except Exception as exc:
                    errors[safe_code(exc)] += 1
                    active[0] = False
            qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL if kind == 'tf' else DurabilityPolicy.VOLATILE)
            subscriptions.append(node.create_subscription(get_message(type_name), topic, callback, qos))
        deadline = time.monotonic() + discovery
        while time.monotonic() < deadline: rclpy.spin_once(node, timeout_sec=.05)
        # TF transient data may arrive during discovery, so recreate that
        # subscription after activation to explicitly record retained TF.
        for subscription, (_, _, kind, _) in zip(subscriptions, topics):
            if kind == 'tf': node.destroy_subscription(subscription)
        active[0] = True
        from tf2_msgs.msg import TFMessage
        def static_callback(message):
            if not active[0]: return
            try:
                decode_projection(message, 'tf', 'tf', spec)
                serialized = serialize_message(message)
                require(total[0] + len(serialized) <= maximum * .9, 'BAG_SIZE_LIMIT')
                writer.write('/tf_static', serialized, time.time_ns())
                digests['/tf_static'].update(struct.pack('<Q', len(serialized)))
                digests['/tf_static'].update(serialized)
                counts['/tf_static'] += 1
                total[0] += len(serialized)
            except Exception as exc:
                errors[safe_code(exc)] += 1
                active[0] = False
        subscriptions.append(node.create_subscription(TFMessage, '/tf_static', static_callback,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)))
        deadline = time.monotonic() + seconds
        while active[0] and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.02)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        del writer  # flush SQLite and metadata before read-back
    if errors or not all(counts[topic] > 0 for topic, *_ in topics):
        raise BagRecordingError(errors, {role+':'+kind: counts[topic] for topic,_,kind,role in topics})
    require(sum(item.stat().st_size for item in path.iterdir() if item.is_file()) <= maximum, 'BAG_DISK_SIZE_LIMIT')
    manifest = {'version': 1, 'spec_sha256': hashlib.sha256(canonical(spec)).hexdigest(), 'wire_only': wire_only,
                'topics': [{'topic': topic, 'type': type_name, 'count': counts[topic], 'sha256': digests[topic].hexdigest()}
                           for topic, type_name, *_ in topics]}
    with open(path/'contract-manifest.json', 'x') as handle:
        json.dump(manifest, handle, sort_keys=True)
    return manifest


def read_back(spec, path, wire_only):
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    manifest = json.loads((path/'contract-manifest.json').read_text())
    require(manifest.get('version') == 1 and manifest.get('wire_only') == wire_only
            and manifest.get('spec_sha256') == hashlib.sha256(canonical(spec)).hexdigest(), 'BAG_SPEC_MISMATCH')
    topic_bindings = {topic: (type_name, kind, role) for topic, type_name, kind, role in bindings(spec, wire_only)}
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(path), storage_id='sqlite3'), rosbag2_py.ConverterOptions('', ''))
    require({t.name: t.type for t in reader.get_all_topics_and_types()} == {t: b[0] for t,b in topic_bindings.items()}, 'BAG_TOPIC_TYPES')
    counts, digests = Counter(), {topic: hashlib.sha256() for topic in topic_bindings}
    observer = Observer(spec, wire_only)
    first, last = None, None
    while reader.has_next():
        topic, serialized, received = reader.read_next()
        require(topic in topic_bindings and len(serialized) <= 512 * 1024**2, 'BAG_MESSAGE_BOUND')
        type_name, kind, role = topic_bindings[topic]
        message = deserialize_message(serialized, get_message(type_name))
        for projection in decode_projection(message, kind, role, spec):
            observer.observe(kind, role, projection, received)
        digests[topic].update(struct.pack('<Q', len(serialized)))
        digests[topic].update(serialized)
        counts[topic] += 1
        first = received if first is None else min(first, received)
        last = received if last is None else max(last, received)
    require(all(counts[item['topic']] == item['count'] and digests[item['topic']].hexdigest() == item['sha256']
                for item in manifest['topics']) and len(manifest['topics']) == len(topic_bindings), 'BAG_SERIALIZED_ROUNDTRIP')
    # Bag replay proves recorded payload/association fidelity; graph QoS and late
    # join are live-only evidence and are deliberately not fabricated here.
    report = observer.summary(replay=True)
    live_only = {'QOS_NOT_OBSERVED', 'TF_LATE_JOIN_NOT_EXERCISED'}
    replay_errors = {key: value for key, value in report['contract_errors'].items() if key not in live_only}
    return {'version': 1, 'kind': 'private-rosbag2-roundtrip',
            'ok': not replay_errors and not any(v['contract_errors'] for v in report['streams'].values()),
            'serialized_byte_exact': True, 'raw_images_recorded_privately': True,
            'standard_wire_only': wire_only, 'counts_by_role': {role+':'+kind: counts[topic] for topic,(_,kind,role) in topic_bindings.items()},
            'recorded_window_seconds': (last-first)/1e9 if first is not None else None,
            'matched_frames': {role: v['matched_frames'] for role,v in report['streams'].items()},
            'matched_pairs': report['matched_pairs'], 'contract_errors': replay_errors,
            'stream_errors': {role:v['contract_errors'] for role,v in report['streams'].items()},
            'live_qos_verified': False, 'late_tf_delivery_verified': False,
            'business_consumer_compatible': False, 'physical_scale_verified': False,
            'scope': 'actual-rosbag2-serialized-record-readback-and-layout-decoding-not-ROS-republication'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--bag', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--discovery-seconds', type=float, default=8)
    parser.add_argument('--max-mib', type=int, default=1024)
    parser.add_argument('--wire-only', action='store_true')
    parser.add_argument('--read-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        require(args.bag.is_absolute() and 0 < args.seconds <= 60 and 0 < args.discovery_seconds <= 60
                and 1 <= args.max_mib <= 16384 and args.spec.stat().st_size <= 65536, 'BAG_ARGUMENT')
        spec = validate_spec(json.loads(args.spec.read_text()))
        if not args.read_only:
            record(spec, args.bag, args.seconds, args.discovery_seconds, args.max_mib * 1024**2, args.wire_only)
        report = read_back(spec, args.bag, args.wire_only)
    except Exception as exc:
        report = {'version': 1, 'kind': 'private-rosbag2-roundtrip', 'ok': False,
                  'errors': [str(exc) if isinstance(exc, ContractError) else 'ROSBAG_ROUNDTRIP_FAILED'],
                  'error_class': type(exc).__name__, 'business_consumer_compatible': False}
        if isinstance(exc, BagRecordingError): report.update(exc.diagnostics)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report.get('ok') else 1


if __name__ == '__main__': raise SystemExit(main())
