#!/usr/bin/env python3
"""Synthetic nonidentity static-TF late-join and pair-evidence ROS wire test."""
import argparse
import ctypes as c
import json
import math
import time
import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from tf2_msgs.msg import TFMessage
from std_msgs.msg import String


class AuxConfig(c.Structure):
    _fields_ = [('abi_version', c.c_uint32), ('struct_size', c.c_uint32), ('node_name', c.c_char_p),
                ('namespace_name', c.c_char_p), ('pair_metadata_topic', c.c_char_p),
                ('parent_frame', c.c_char_p), ('child_frame', c.c_char_p),
                ('stamp_sec', c.c_int32), ('stamp_nanosec', c.c_uint32),
                ('rotation_row_major', c.c_double * 9), ('translation_m', c.c_double * 3)]


class Pair(c.Structure):
    _fields_ = [('abi_version', c.c_uint32), ('struct_size', c.c_uint32),
                ('pair_sequence', c.c_uint64), ('color_sequence', c.c_uint64), ('depth_sequence', c.c_uint64),
                ('color_stamp_ns', c.c_uint64), ('depth_stamp_ns', c.c_uint64),
                ('color_timestamp_us', c.c_uint64), ('depth_timestamp_us', c.c_uint64), ('delta_us', c.c_uint64),
                ('clock', c.c_uint32), ('same_frameset', c.c_uint32), ('hardware_sync_verified', c.c_uint32)]


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True)
    parser.add_argument('--namespace', required=True)
    args = parser.parse_args()
    require(args.library.startswith('/') and args.namespace.startswith('/') and not args.namespace.endswith('/'), 'explicit absolute library/namespace required')
    library = c.CDLL(args.library)
    library.gdp_ros_aux_abi_version.restype = c.c_uint32
    library.gdp_ros_aux_create.argtypes = [c.POINTER(AuxConfig), c.POINTER(c.c_void_p)]
    library.gdp_ros_aux_create.restype = c.c_int32
    library.gdp_ros_aux_publish_pair.argtypes = [c.c_void_p, c.POINTER(Pair)]
    library.gdp_ros_aux_publish_pair.restype = c.c_int32
    library.gdp_ros_aux_destroy.argtypes = [c.c_void_p]
    library.gdp_ros_aux_destroy.restype = None
    require(library.gdp_ros_aux_abi_version() == 1, 'aux ABI mismatch')
    pair_topic = args.namespace + '/rgbd/pair_metadata'
    parent, child = 'synthetic_aux_color_optical', 'synthetic_aux_depth_optical'
    micros = time.time_ns() // 1000
    config = AuxConfig(1, c.sizeof(AuxConfig), b'synthetic_aux_source', args.namespace.encode(), pair_topic.encode(),
                       parent.encode(), child.encode(), micros // 1_000_000, (micros % 1_000_000) * 1000,
                       (c.c_double * 9)(0., -1., 0., 1., 0., 0., 0., 0., 1.), (c.c_double * 3)(.1, .2, .3))
    handle = c.c_void_p()
    original_rotation = tuple(config.rotation_row_major)
    config.rotation_row_major[:] = (-1., 0., 0., 0., 1., 0., 0., 0., 1.)
    require(library.gdp_ros_aux_create(c.byref(config), c.byref(handle)) == 1 and not handle.value, 'reflection accepted')
    config.rotation_row_major[:] = original_rotation
    require(library.gdp_ros_aux_create(c.byref(config), c.byref(handle)) == 0 and handle.value, 'aux creation failed')
    # Join after the one-time static publication. Transient local must retain it.
    time.sleep(.5)
    context = Context()
    rclpy.init(context=context)
    node = Node('synthetic_aux_late_sink', namespace=args.namespace, context=context, use_global_arguments=False)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    transforms, pairs = [], []
    try:
        tf_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        pair_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE)

        def receive_tf(message):
            transforms.extend(t for t in message.transforms if t.header.frame_id == parent and t.child_frame_id == child)

        tf_sub = node.create_subscription(TFMessage, '/tf_static', receive_tf, tf_qos)
        pair_sub = node.create_subscription(String, pair_topic, lambda m: pairs.append(json.loads(m.data)), pair_qos)
        deadline = time.monotonic() + 8.
        while time.monotonic() < deadline and not transforms:
            executor.spin_once(timeout_sec=.05)
        require(transforms, 'late subscriber did not receive retained tf_static')
        transform = transforms[0]
        t, q = transform.transform.translation, transform.transform.rotation
        require(all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip((t.x, t.y, t.z), (.1, .2, .3))), 'translation/direction differs')
        require(math.isclose(q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w, 1., abs_tol=1e-12), 'quaternion not unit')
        # Quaternion rotation expanded independently; an inverted transform fails.
        rotation = (1-2*(q.y*q.y+q.z*q.z), 2*(q.x*q.y-q.z*q.w), 2*(q.x*q.z+q.y*q.w),
                    2*(q.x*q.y+q.z*q.w), 1-2*(q.x*q.x+q.z*q.z), 2*(q.y*q.z-q.x*q.w),
                    2*(q.x*q.z-q.y*q.w), 2*(q.y*q.z+q.x*q.w), 1-2*(q.x*q.x+q.y*q.y))
        source, translation = (1., 2., 3.), (t.x, t.y, t.z)
        target = tuple(sum(rotation[row*3+col] * source[col] for col in range(3)) + translation[row] for row in range(3))
        require(all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(target, (-1.9, 1.2, 3.3))), 'child-to-parent known point failed')
        inverse = tuple(sum(rotation[row*3+col] * (target[row]-translation[row]) for row in range(3)) for col in range(3))
        require(all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(inverse, source)), 'inverse transform failed')
        for endpoint in node.get_publishers_info_by_topic('/tf_static'):
            if endpoint.node_name == 'synthetic_aux_source':
                require(endpoint.qos_profile.reliability == ReliabilityPolicy.RELIABLE and endpoint.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL, 'tf graph QoS differs')
        for sequence in range(1, 11):
            # Selected pairing timestamps must advance positively; actual image stamps remain distinct.
            pair = Pair(1, c.sizeof(Pair), sequence, sequence+100, sequence+200,
                        micros*1000+sequence*2000, micros*1000+sequence*2000+1000,
                        (sequence-1)*100+1, (sequence-1)*100+11, 10, 1, 1, 0)
            if sequence == 1:
                pair.hardware_sync_verified = 1
                require(library.gdp_ros_aux_publish_pair(handle, c.byref(pair)) == 1, 'fabricated hardware sync accepted')
                pair.hardware_sync_verified = 0
            require(library.gdp_ros_aux_publish_pair(handle, c.byref(pair)) == 0, 'pair metadata publish failed')
            end = time.monotonic() + .05
            while time.monotonic() < end:
                executor.spin_once(timeout_sec=.01)
        deadline = time.monotonic() + 2.
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=.05)
        require(len(pairs) >= 3, 'pair metadata not received')
        last = 0
        for pair in pairs:
            sequence = pair['pair_sequence']
            require(sequence > last and pair['color_sequence'] == sequence+100 and pair['depth_sequence'] == sequence+200
                    and pair['color_stamp_ns'] == micros*1000+sequence*2000 and pair['depth_stamp_ns'] == micros*1000+sequence*2000+1000
                    and pair['color_timestamp_us'] == (sequence-1)*100+1 and pair['depth_timestamp_us'] == (sequence-1)*100+11
                    and pair['delta_us'] == 10 and pair['clock'] == 'device' and pair['same_frameset'] is True
                    and pair['hardware_sync_verified'] is False and pair['provenance'] == 'sdkframeset'
                    and pair['publisher_qos_verified'] is True, 'pair association evidence differs')
            last = sequence
        print(json.dumps({'ok': True, 'kind': 'synthetic-aux-wire-smoke', 'late_static_tf_received': True,
                          'known_point_forward_inverse_match': True, 'pair_metadata_received': len(pairs),
                          'hardware_verified': False, 'physical_calibration_verified': False}))
        del tf_sub, pair_sub
    finally:
        executor.shutdown()
        node.destroy_node()
        context.shutdown()
        library.gdp_ros_aux_destroy(handle)


if __name__ == '__main__':
    main()
