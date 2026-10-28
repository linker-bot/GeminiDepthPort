#!/usr/bin/env python3
"""Real ROS late-join regression with four synthetic writers, without SDK access."""
import argparse
import ctypes as c
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime.qos_contract import STATIC_TF_SUBSCRIPTION_DEPTH
from smoke_aux import AuxConfig
import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from tf2_msgs.msg import TFMessage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True)
    args = parser.parse_args()
    library = c.CDLL(args.library)
    library.gdp_ros_aux_create.argtypes = [c.POINTER(AuxConfig), c.POINTER(c.c_void_p)]
    library.gdp_ros_aux_create.restype = c.c_int
    library.gdp_ros_aux_destroy.argtypes = [c.c_void_p]
    handles, subscriptions = [], []
    node = None
    initialized = False
    try:
        for index in range(4):
            now = time.time_ns()
            namespace = ('/tfdiag/cam' + str(index)).encode()
            config = AuxConfig(
                1, c.sizeof(AuxConfig), ('tf_source' + str(index)).encode(), namespace,
                namespace + b'/pair', ('diag_color' + str(index)).encode(),
                ('diag_depth' + str(index)).encode(), now // 10**9, now % 10**9,
                (c.c_double * 9)(1, 0, 0, 0, 1, 0, 0, 0, 1),
                (c.c_double * 3)(.1, 0, 0))
            handle = c.c_void_p()
            code = library.gdp_ros_aux_create(c.byref(config), c.byref(handle))
            if handle.value:
                handles.append(handle)
            assert code == 0 and handle.value, 'TF_SOURCE_CREATE_FAILED'
        time.sleep(.5)  # Readers join after each source's one-time publication.
        rclpy.init()
        initialized = True
        node = rclpy.create_node('tf_queue_late_join_regression')
        seen = {1: set(), STATIC_TF_SUBSCRIPTION_DEPTH: set()}
        for depth in sorted(seen):
            qos = QoSProfile(depth=depth, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
            subscriptions.append(node.create_subscription(
                TFMessage, '/tf_static',
                lambda message, depth=depth: seen[depth].update(
                    transform.child_frame_id for transform in message.transforms), qos))
        # Simulate an executor busy with image callbacks while retained TF arrives.
        time.sleep(2)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.05)
        assert seen[STATIC_TF_SUBSCRIPTION_DEPTH] == {
            'diag_depth' + str(index) for index in range(4)
        }, 'MULTI_PUBLISHER_TF_MISSING'
        print(json.dumps({'version': 1, 'ok': True, 'synthetic_publishers': 4,
                          'control_depth_1_received': len(seen[1]),
                          'configured_subscription_depth': STATIC_TF_SUBSCRIPTION_DEPTH,
                          'configured_received': len(seen[STATIC_TF_SUBSCRIPTION_DEPTH])}))
    finally:
        if node:
            node.destroy_node()
        if initialized:
            rclpy.shutdown()
        for handle in handles:
            library.gdp_ros_aux_destroy(handle)


if __name__ == '__main__':
    main()
