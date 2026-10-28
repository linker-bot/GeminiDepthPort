#!/usr/bin/env python3
"""Read-only ROS decoder. All association and quality decisions belong to Rust."""
import argparse
import json
import os
import selectors
import select
import subprocess
import tempfile
import time

LIMIT = 65536



def parse_config(raw):
    """Reject ambiguous/non-JSON input before reserializing the Rust config."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("CONFIG_DUPLICATE_KEY")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("CONFIG_NONFINITE")

    return json.loads(raw, object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)


def stamp(header):
    return {"sec": int(header.stamp.sec), "nanosec": int(header.stamp.nanosec)}


def image(message):
    return dict(stamp=stamp(message.header), frame_id=str(message.header.frame_id),
                width=int(message.width), height=int(message.height), step=int(message.step),
                encoding=str(message.encoding), is_bigendian=int(message.is_bigendian),
                payload_len=len(message.data))


def camera_info(message):
    return dict(stamp=stamp(message.header), frame_id=str(message.header.frame_id),
                width=int(message.width), height=int(message.height),
                distortion_model=str(message.distortion_model),
                k=list(map(float, message.k)), d=list(map(float, message.d)),
                r=list(map(float, message.r)), p=list(map(float, message.p)),
                binning=[int(message.binning_x), int(message.binning_y)],
                roi=[int(message.roi.x_offset), int(message.roi.y_offset),
                     int(message.roi.width), int(message.roi.height)],
                roi_do_rectify=bool(message.roi.do_rectify))


def metadata(message):
    # Only extract the declared wire header. Rust checks it against strict JSON.
    raw = str(message.data)
    if len(raw.encode()) > 4096:
        return {"stamp": {"sec": 0, "nanosec": 0}, "data": ""}
    try:
        value = json.loads(raw)
        header = {"sec": int(value["stamp_sec"]), "nanosec": int(value["stamp_nanosec"])}
    except (ValueError, TypeError, KeyError, OverflowError):
        header = {"sec": 0, "nanosec": 0}
    return {"stamp": header, "data": raw}


class Engine:
    def __init__(self, executable, config, timeout=5.0):
        self.timeout = timeout
        self.buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf8") as private:
            json.dump(config, private, allow_nan=False)
            private.flush()
            self.process = subprocess.Popen([executable, "--config", private.name],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            # A query is a startup handshake before the private file disappears.
            try:
                self.call({"kind": "query", "request": {"version": 1, "query": {"kind": "snapshot"}}})
            except Exception:
                self.close()
                raise

    def call(self, command):
        line = json.dumps({"version": 1, "command": command}, allow_nan=False).encode() + b"\n"
        if len(line) > LIMIT:
            raise RuntimeError("ADAPTER_INPUT_LIMIT")
        deadline = time.monotonic() + self.timeout
        pending = memoryview(line)
        while pending:
            if not select.select([], [self.process.stdin], [], max(0, deadline-time.monotonic()))[1]:
                raise RuntimeError("OBSERVER_TIMEOUT")
            try:
                sent = os.write(self.process.stdin.fileno(), pending)
            except BlockingIOError:
                continue
            pending = pending[sent:]
        while b"\n" not in self.buffer:
            if not self.selector.select(max(0, deadline - time.monotonic())):
                raise RuntimeError("OBSERVER_TIMEOUT")
            chunk = os.read(self.process.stdout.fileno(), 8192)
            if not chunk:
                raise RuntimeError("OBSERVER_CLOSED")
            self.buffer.extend(chunk)
            if len(self.buffer) > 4 * 1024 * 1024:
                raise RuntimeError("OBSERVER_RESPONSE_LIMIT")
        raw, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        response = json.loads(raw)
        if not response.get("ok"):
            raise RuntimeError("OBSERVER_REQUEST_REJECTED")
        return response

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def run(args):
    # ROS imports are confined to the actual runtime; tests need no ROS or SDK.
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    from sensor_msgs.msg import Image, CameraInfo
    from std_msgs.msg import String
    with open(args.config, encoding="utf8") as source:
        raw = source.read(LIMIT + 1)
        if len(raw.encode()) > LIMIT:
            raise RuntimeError("CONFIG_LIMIT")
        config = parse_config(raw)
    bindings = config["bindings"]
    topics = []
    for role in ("color", "depth"):
        for key in ("image", "camera_info", "metadata"):
            topic = bindings[role].get(key)
            if topic is not None:
                if not isinstance(topic, str) or not topic.startswith("/"):
                    raise RuntimeError("BINDING_INVALID")
                topics.append(topic)
    if len(topics) != len(set(topics)):
        raise RuntimeError("BINDING_DUPLICATE")
    qos_config = config["qos"]
    reliability = {"reliable": ReliabilityPolicy.RELIABLE,
                   "best-effort": ReliabilityPolicy.BEST_EFFORT}[qos_config["reliability"]]
    depth = qos_config["depth"]
    if type(depth) is not int or not 1 <= depth <= 4096:
        raise RuntimeError("QOS_INVALID")
    qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=depth,
                     reliability=reliability, durability=DurabilityPolicy.VOLATILE)
    session = config["engine_config"]["streams"]["color"]["context"]["session_id"]
    engine = Engine(args.engine, config["engine_config"])
    rclpy.init(args=[])
    node = rclpy.create_node("capture_read_only_observer")
    origin = time.monotonic_ns()
    def at():
        return {"session_id": session, "offset_ns": time.monotonic_ns() - origin}
    def callback(role, kind, decoder, field):
        def receive(message):
            command = {"kind": kind, "stream": role, "at": at(), field: decoder(message)}
            if kind == "image":
                command["received_unix_ns"] = time.time_ns()
            engine.call(command)
        return receive
    try:
        for role in ("color", "depth"):
            for topic_key, msg_type, kind, decoder, field in (
                ("image", Image, "image", image, "image"),
                ("camera_info", CameraInfo, "camera-info", camera_info, "info"),
                ("metadata", String, "metadata", metadata, "metadata")):
                if bindings[role].get(topic_key):
                    node.create_subscription(msg_type, bindings[role][topic_key],
                                             callback(role, kind, decoder, field), qos)
        node.create_timer(args.tick_seconds, lambda: engine.call(
            {"kind": "tick", "at": at(), "received_unix_ns": time.time_ns()}))
        deadline = time.monotonic() + args.duration_seconds
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=min(args.tick_seconds, max(0, deadline-time.monotonic())))
        engine.call({"kind": "finalize"})
        print(json.dumps(engine.call({"kind": "query", "request": {
            "version": 1, "query": {"kind": "snapshot"}}}), allow_nan=False))
    finally:
        node.destroy_node()
        rclpy.shutdown()
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--tick-seconds", type=float, required=True)
    args = parser.parse_args()
    if not 0 < args.tick_seconds <= args.duration_seconds or not args.duration_seconds <= 86400:
        parser.error("invalid duration or tick interval")
    try:
        run(args)
    except Exception:
        print(json.dumps({"version": 1, "ok": False, "error": {"code": "ROS_OBSERVER_FAILED"}}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
