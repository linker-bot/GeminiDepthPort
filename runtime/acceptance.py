"""Controlled ROS consumer acceptance. No SDK device access or public identities."""
import argparse
import json
import math
import statistics
import struct
import time
from pathlib import Path
try:
    from .qos_contract import validate_offered_qos
except ImportError:
    from qos_contract import validate_offered_qos


class AcceptanceError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise AcceptanceError(code)


def validate_spec(spec):
    require(isinstance(spec, dict) and spec.get('version') == 1, 'SPEC_VERSION')
    streams = spec.get('streams')
    require(isinstance(streams, list) and bool(streams), 'NO_STREAMS')
    require(all(isinstance(s, dict) and isinstance(s.get('topic'), str) for s in streams), 'STREAM_INVALID')
    require(len({s['topic'] for s in streams}) == len(streams), 'DUPLICATE_STREAM')
    for s in streams:
        require(isinstance(s.get('topic'), str) and s['topic'].startswith('/'), 'TOPIC_INVALID')
        require(s.get('encoding') in ('yuyv', '16UC1'), 'ENCODING_UNSUPPORTED')
        require(all(type(s.get(k)) is int and s[k] > 0 for k in ('width', 'height')), 'DIMENSIONS_INVALID')
        require(isinstance(s.get('frame_id'), str) and bool(s['frame_id']), 'FRAME_REQUIRED')
        require(s.get('required_reliability') in ('reliable', 'best_effort'), 'QOS_REQUIRED')
        timing = s.get('time', {})
        require(isinstance(timing, dict), 'TIME_MAPPING_REQUIRED')
        require(timing.get('source') in ('device', 'host', 'global'), 'TIME_SOURCE_REQUIRED')
        require(timing.get('common_clock') == 'unix', 'TIME_BASE_REQUIRED')
        require(type(timing.get('offset_ns')) is int, 'TIME_MAPPING_REQUIRED')
        require(timing.get('mapping_evidence') in ('measured', 'driver_host_clock'), 'TIME_EVIDENCE_REQUIRED')
        require(timing['mapping_evidence'] != 'driver_host_clock' or
                (timing['source'] == 'host' and timing['offset_ns'] == 0), 'TIME_EVIDENCE_MISMATCH')
        for key in ('max_offset_seconds', 'max_gap_seconds', 'freshness_seconds'):
            require(type(s.get(key)) in (float, int) and math.isfinite(s[key]) and s[key] > 0, 'LIMIT_INVALID')
        require(type(s.get('min_frames')) is int and s['min_frames'] >= 2, 'FRAME_COUNT_INVALID')
        if s['encoding'] == '16UC1':
            require(s.get('depth_unit') == 'mm' and s.get('depth_alignment') == 'raw', 'DEPTH_SEMANTICS_REQUIRED')
            reference = s.get('depth_reference')
            require(isinstance(reference, dict), 'DEPTH_REFERENCE_REQUIRED')
            require(reference.get('method') == 'known_distance', 'DEPTH_REFERENCE_REQUIRED')
            require(all(type(reference.get(k)) is int and 0 <= reference[k] < s[axis]
                        for k, axis in (('x', 'width'), ('y', 'height'))), 'DEPTH_PIXEL_INVALID')
            for key in ('distance_mm', 'tolerance_mm'):
                require(type(reference.get(key)) in (int, float) and math.isfinite(reference[key])
                        and reference[key] > 0, 'DEPTH_REFERENCE_INVALID')
    return spec


def image_sample(msg, expected, receive_ns):
    """Inspect actual bytes. Zero depth is invalid; rows may contain padding."""
    width, height, step = msg.width, msg.height, msg.step
    require(msg.encoding == expected['encoding'], 'ENCODING_MISMATCH')
    require((width, height) == (expected['width'], expected['height']), 'DIMENSION_MISMATCH')
    require(msg.header.frame_id == expected['frame_id'], 'FRAME_MISMATCH')
    require(step >= width * 2 and len(msg.data) == height * step, 'PAYLOAD_LAYOUT')
    require(msg.is_bigendian in (0, 1), 'ENDIAN_INVALID')
    if msg.encoding == 'yuyv':
        require(width % 2 == 0 and step == width * 2, 'YUYV_LAYOUT')
    else:
        require(step % 2 == 0, 'DEPTH_LAYOUT')
    stamp = msg.header.stamp
    require(type(stamp.sec) is int and type(stamp.nanosec) is int
            and stamp.sec >= 0 and 0 <= stamp.nanosec < 1_000_000_000, 'STAMP_INVALID')
    ns = stamp.sec * 1_000_000_000 + stamp.nanosec
    require(ns > 0, 'STAMP_ZERO')
    sample = {'stamp_ns': ns, 'receive_ns': receive_ns}
    if msg.encoding == '16UC1':
        reference = expected['depth_reference']
        position = reference['y'] * step + reference['x'] * 2
        sample['reference_depth'] = struct.unpack_from('>H' if msg.is_bigendian else '<H', bytes(msg.data), position)[0]
    return sample


def qos_compatible(publishers, required):
    """Match actual offers to the requested subscriber, rejecting ambiguity."""
    if required == 'reliable':
        return validate_offered_qos(publishers, required)['ready']
    return len(publishers) == 1 and all(
        p.get('reliability') in ('reliable', 'best_effort')
        and p.get('durability') in ('volatile', 'transient_local') for p in publishers)


def evaluate(expected, samples, publishers, now_ns, errors=()):
    failures = set(errors)
    if not qos_compatible(publishers, expected['required_reliability']):
        failures.add('QOS_INCOMPATIBLE')
    if len(publishers) != 1:
        failures.add('PUBLISHER_COUNT')
    if len(samples) < expected['min_frames']:
        failures.add('INSUFFICIENT_FRAMES')
    if samples:
        stamps = [s['stamp_ns'] for s in samples]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        if any(g <= 0 for g in gaps): failures.add('STAMP_NOT_ADVANCING')
        if any(g > expected['max_gap_seconds'] * 1e9 for g in gaps): failures.add('STAMP_GAP')
        mapped = [s['stamp_ns'] + expected['time']['offset_ns'] for s in samples]
        offsets = [s['receive_ns'] - t for s, t in zip(samples, mapped)]
        if any(abs(offset) > expected['max_offset_seconds'] * 1e9 for offset in offsets):
            failures.add('TIME_BASE_OFFSET')
        if now_ns - samples[-1]['receive_ns'] > expected['freshness_seconds'] * 1e9:
            failures.add('RECEIVE_STALE')
        if not -expected['max_offset_seconds'] * 1e9 <= now_ns - mapped[-1] <= expected['freshness_seconds'] * 1e9:
            failures.add('SOURCE_STALE')
        if expected['encoding'] == '16UC1':
            depths = [s.get('reference_depth', 0) for s in samples]
            valid = [d for d in depths if d > 0]
            reference = expected['depth_reference']
            if len(valid) < expected['min_frames']:
                failures.add('DEPTH_REFERENCE_INVALID_SAMPLES')
            elif abs(statistics.median(valid) - reference['distance_mm']) > reference['tolerance_mm']:
                failures.add('DEPTH_SCALE_REFERENCE_MISMATCH')
    return {'ok': not failures, 'errors': sorted(failures), 'frames': len(samples),
            'qos_observed': bool(publishers), 'hardware_exposure_sync_verified': False,
            'depth_alignment_verified': False if expected['encoding'] == '16UC1' else None}


def run_live(spec, duration, discovery):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    from sensor_msgs.msg import Image
    rclpy.init()
    node = rclpy.create_node('consumer_acceptance_probe')
    streams = spec['streams']
    samples, errors = [[] for _ in streams], [set() for _ in streams]
    active = [False]
    subscriptions = []
    try:
        for index, stream in enumerate(streams):
            def callback(msg, index=index, stream=stream):
                if not active[0]: return
                try: samples[index].append(image_sample(msg, stream, time.time_ns()))
                except AcceptanceError as exc: errors[index].add(str(exc))
                except (AttributeError, TypeError, ValueError, struct.error): errors[index].add('MESSAGE_MALFORMED')
            qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=10,
                             reliability=ReliabilityPolicy.RELIABLE if stream['required_reliability'] == 'reliable'
                             else ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
            subscriptions.append(node.create_subscription(Image, stream['topic'], callback, qos))
        end = time.monotonic() + discovery
        while time.monotonic() < end: rclpy.spin_once(node, timeout_sec=0.1)
        active[0] = True
        end = time.monotonic() + duration
        while time.monotonic() < end: rclpy.spin_once(node, timeout_sec=0.1)
        active[0] = False
        now = time.time_ns()
        results = []
        for index, stream in enumerate(streams):
            offers = []
            for endpoint in node.get_publishers_info_by_topic(stream['topic']):
                q = endpoint.qos_profile
                offers.append({'reliability': 'reliable' if q.reliability == ReliabilityPolicy.RELIABLE else
                               'best_effort' if q.reliability == ReliabilityPolicy.BEST_EFFORT else 'unknown',
                               'durability': 'volatile' if q.durability == DurabilityPolicy.VOLATILE else
                               'transient_local' if q.durability == DurabilityPolicy.TRANSIENT_LOCAL else 'other'})
            result = evaluate(stream, samples[index], offers, now, errors[index])
            results.append(dict(result, stream_index=index))
        return {'version': 1, 'kind': 'controlled-consumer-acceptance',
                'ok': all(r['ok'] for r in results), 'streams': results,
                'scope': 'observed_transport_layout_timing_and_reference_depth_only',
                'calibration_verified': False, 'hardware_exposure_sync_verified': False,
                'cross_distribution_compatibility_certified': False}
    finally:
        node.destroy_node()
        rclpy.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True, help='Private controlled acceptance specification')
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--discovery-seconds', type=float, default=3)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        require(math.isfinite(args.seconds) and 0 < args.seconds <= 300, 'WINDOW_INVALID')
        require(math.isfinite(args.discovery_seconds) and 0 <= args.discovery_seconds <= 60, 'WINDOW_INVALID')
        spec = validate_spec(json.loads(args.spec.read_text()))
        report = {'version': 1, 'spec_valid': True, 'hardware_verified': False} if args.validate_only else run_live(spec, args.seconds, args.discovery_seconds)
    except AcceptanceError as exc:
        report = {'version': 1, 'ok': False, 'errors': [str(exc)]}
    except Exception:
        report = {'version': 1, 'ok': False, 'errors': ['ACCEPTANCE_UNAVAILABLE']}
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get('ok', report.get('spec_valid', False)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
