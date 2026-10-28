#!/usr/bin/env python3
"""Public synthetic DDS control. No SDK, USB, private config or calibration."""
import argparse
import array
import hashlib
import json
import os
import re
import time

import rclpy
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image
from std_msgs.msg import String


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("pub", "sub"))
    parser.add_argument("--expect-no-data", action="store_true")
    args = parser.parse_args()
    payload = bytes(range(256)) * 2400  # 640 × 480 × 2 = 614,400 bytes.
    expected_digest = hashlib.sha256(payload).digest()
    expected = set(range(1, 25))
    images, strings, errors = set(), set(), []
    rclpy.init()
    node = rclpy.create_node("gdp_cross_container_" + args.role)
    qos = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=32,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )
    token = os.environ.get("GDP_DDS_CONTROL_RUN", "manual_control")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]{0,63}", token):
        raise ValueError("invalid public control run token")
    prefix = "/gdp_public_dds_control/" + token
    started = time.monotonic()
    try:
        if args.role == "sub":
            def image_callback(message):
                valid = (
                    message.width == 640 and message.height == 480
                    and message.step == 1280 and message.encoding == "16UC1"
                    and not message.is_bigendian
                    and message.header.frame_id == "synthetic_depth_optical_frame"
                    and message.header.stamp.sec == 1234
                    and len(message.data) == 614400
                    and hashlib.sha256(message.data).digest() == expected_digest
                )
                if valid:
                    images.add(message.header.stamp.nanosec)
                else:
                    errors.append("IMAGE_BYTES_OR_LAYOUT")

            def string_callback(message):
                try:
                    prefix, sequence = message.data.rsplit(":", 1)
                    if prefix != "gdp-public-dds-control":
                        raise ValueError()
                    strings.add(int(sequence))
                except ValueError:
                    errors.append("STRING_CONTENT")

            subscriptions = [
                node.create_subscription(Image, prefix + "/image", image_callback, qos),
                node.create_subscription(String, prefix + "/string", string_callback, qos),
            ]
            while time.monotonic() - started < 14:
                rclpy.spin_once(node, timeout_sec=0.05)
                if not args.expect_no_data and images == expected and strings == expected:
                    break
            # Keep handles alive until after the spin loop.
            assert len(subscriptions) == 2
            ok = not errors and (
                not images and not strings if args.expect_no_data
                else images == expected and strings == expected
            )
            report = dict(role="sub", ok=ok, images=len(images), strings=len(strings),
                          coherent_sequences=len(images & strings), payload_bytes=614400,
                          errors=sorted(set(errors)), expected_no_data=args.expect_no_data)
        else:
            image_pub = node.create_publisher(Image, prefix + "/image", qos)
            string_pub = node.create_publisher(String, prefix + "/string", qos)
            while time.monotonic() - started < 9:
                rclpy.spin_once(node, timeout_sec=0.05)
                if image_pub.get_subscription_count() and string_pub.get_subscription_count():
                    break
            matched = bool(image_pub.get_subscription_count() and string_pub.get_subscription_count())
            if not matched:
                report = dict(role="pub", ok=False, error="NO_MATCHED_SUBSCRIBERS")
            else:
                image = Image()
                image.width, image.height, image.step = 640, 480, 1280
                image.encoding, image.is_bigendian = "16UC1", False
                image.header.frame_id = "synthetic_depth_optical_frame"
                image.header.stamp.sec = 1234
                image.data = array.array("B", payload)
                for sequence in sorted(expected):
                    image.header.stamp.nanosec = sequence
                    text = String()
                    text.data = "gdp-public-dds-control:" + str(sequence)
                    image_pub.publish(image)
                    string_pub.publish(text)
                    rclpy.spin_once(node, timeout_sec=0.05)
                    time.sleep(0.05)
                # Leave reliable writers alive long enough for fragmented payload ACKs.
                end = time.monotonic() + 0.5
                while time.monotonic() < end:
                    rclpy.spin_once(node, timeout_sec=0.05)
                report = dict(role="pub", ok=True, images_published=24,
                              strings_published=24, matched=True, payload_bytes=614400)
        print(json.dumps(report), flush=True)
        return 0 if report["ok"] else 1
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
