#!/usr/bin/env python3
"""Private ROSbag2/MCAP -> evidence-bound directory adapter. No SDK device access."""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

MAX_JSON = 4 * 1024 * 1024
MAX_MESSAGE = 64 * 1024 * 1024


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("DUPLICATE_JSON_KEY")
            value[key] = item
        return value
    def invalid(_):
        raise ValueError("NONFINITE_JSON")
    def finite_float(raw_number):
        value = float(raw_number)
        if not math.isfinite(value):
            raise ValueError("NONFINITE_JSON")
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite_float)


def json_bytes(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def read_json(path):
    with open(path, "rb") as source:
        raw = source.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("JSON_LIMIT")
    return strict_json(raw), raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_boundary():
    path = Path(__file__).resolve().parents[1] / "observation" / "official_ros_observer.py"
    spec = importlib.util.spec_from_file_location("ros_decode_boundary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BagReader:
    """Optional ROS dependency boundary. CDR bytes are retained before decoding."""
    def __init__(self, uri, storage_id, topics):
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
        self.deserialize = deserialize_message
        self.reader = rosbag2_py.SequentialReader()
        self.reader.open(rosbag2_py.StorageOptions(uri=str(uri), storage_id=storage_id),
                         rosbag2_py.ConverterOptions("cdr", "cdr"))
        advertised = {item.name: item.type for item in self.reader.get_all_topics_and_types()}
        if any(advertised.get(topic) != kind for topic, kind in topics.items()):
            raise ValueError("BAG_TOPIC_TYPE_BINDING")
        self.types = {topic: (kind, get_message(kind)) for topic, kind in topics.items()}
        self.reader.set_filter(rosbag2_py.StorageFilter(topics=list(topics)))

    def __iter__(self):
        while self.reader.has_next():
            topic, cdr, timestamp = self.reader.read_next()
            if topic not in self.types:
                continue
            if len(cdr) > MAX_MESSAGE or timestamp <= 0:
                raise ValueError("BAG_MESSAGE_LIMIT_OR_TIME")
            kind, cls = self.types[topic]
            yield topic, kind, int(timestamp), bytes(cdr), self.deserialize(cdr, cls)


class RecordingWriter:
    """Disk index, bounded SQLite cache and blocking writes; never a pixel queue."""
    def __init__(self, output, snapshot_raw):
        self.output = Path(output)
        self.stage = self.output.with_name(self.output.name + ".partial")
        if self.output.exists() or self.stage.exists():
            raise ValueError("OUTPUT_EXISTS")
        self.stage.mkdir(mode=0o700, parents=False)
        self.snapshot = strict_json(snapshot_raw)
        self.put("snapshot.json", snapshot_raw)
        self.db = sqlite3.connect(self.stage / "lookup.sqlite3")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("CREATE TABLE components(id INTEGER PRIMARY KEY,role TEXT,kind TEXT,stamp INTEGER,descriptor TEXT)")
        self.db.execute("CREATE INDEX image_lookup ON components(role,kind,stamp)")
        self.records = open(self.stage / "records.jsonl", "xb")
        self.wire = open(self.stage / "wire.jsonl", "xb")
        self.hash = hashlib.sha256()
        self.count = 0
        self.message_count = 0

    def put(self, name, raw):
        if len(raw) > MAX_MESSAGE:
            raise ValueError("RECORDING_MESSAGE_LIMIT")
        path = self.stage / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        return {"path": name, "sha256": digest(raw)}

    def archive(self, topic, kind, timestamp, cdr):
        self.message_count += 1
        ref = self.put(f"wire-{self.message_count:020d}.cdr", cdr)
        self.wire.write(json_bytes(dict(topic=topic, type=kind, record_timestamp_ns=timestamp, cdr=ref)) + b"\n")
        return self.message_count

    def component(self, token, role, kind, stamp, descriptor):
        self.db.execute("INSERT INTO components VALUES(?,?,?,?,?)",
                        (token, role, kind, stamp, json_bytes(descriptor).decode()))
        self.db.commit()

    def token(self, token, role, kind):
        row = self.db.execute("SELECT descriptor FROM components WHERE id=? AND role=? AND kind=?",
                              (token, role, kind)).fetchone()
        if row is None:
            raise ValueError("RUST_COMPONENT_TOKEN_MISSING")
        return strict_json(row[0])

    def sdk_image(self, role, timestamp):
        # Exact reference resolution from the recorded SDK pair; no proximity join.
        rows = self.db.execute("SELECT descriptor FROM components WHERE role=? AND kind='image' AND stamp=? LIMIT 2",
                               (role, timestamp)).fetchall()
        if len(rows) != 1:
            raise ValueError("SDK_REFERENCED_IMAGE_MISSING_OR_AMBIGUOUS")
        return strict_json(rows[0][0])

    def record(self, evidence, color, depth, representation):
        sequence = evidence["pair"]["pair_sequence"] if evidence["kind"] == "sdk-bound-pair" else evidence["association"]["association_sequence"]
        raw = json_bytes(evidence)
        ref = self.put(f"evidence-{sequence:020d}.json", raw)
        line = json_bytes(dict(sequence=sequence, evidence=ref, color=color, depth=depth,
                               storage_representation=representation)) + b"\n"
        self.records.write(line)
        self.hash.update(line)
        self.count += 1

    def finish(self, offline_engine, representation):
        self.records.flush()
        os.fsync(self.records.fileno())
        self.wire.flush()
        os.fsync(self.wire.fileno())
        manifest = dict(version=2, source_kind="recording-directory-v2", snapshot="snapshot.json",
                        binding=self.snapshot["content"]["binding"], records="records.jsonl",
                        records_sha256=self.hash.hexdigest(), record_count=self.count)
        self.put("manifest.json", json_bytes(manifest))
        args = [offline_engine, "--input", str(self.stage.resolve()), "--recording-v2", "--validate-only"]
        if representation.get("representation") == "sdk-counts":
            args.append("--preserve-counts")
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            raise ValueError("RUST_RECORDING_VALIDATION_FAILED")
        self.put("COMPLETE.json", json_bytes(dict(version=1, complete=True, records=self.count,
                                                  messages=self.message_count, business_validity=None)))
        self.close()
        self.stage.rename(self.output)

    def close(self):
        self.records.close()
        self.wire.close()
        self.db.close()

    def failed(self):
        try:
            self.put("FAILED.json", json_bytes(dict(version=1, complete=False, business_validity=None)))
        finally:
            self.close()


def import_messages(messages, config, snapshot_raw, writer, observer_engine):
    boundary = load_boundary()
    backend = config["backend"]
    engine = boundary.Engine(observer_engine, config["observer_config"]) if backend == "official" else None
    snapshot = strict_json(snapshot_raw)
    representation = config["storage_representation"]
    roles = {}
    for role in ("color", "depth"):
        for kind, topic in config["bindings"][role].items():
            if kind not in ("image", "camera_info", "metadata") or topic in roles:
                raise ValueError("BINDING_INVALID")
            roles[topic] = (role, kind)
    origin = None
    last = 0
    pending_sdk = None
    session = snapshot["content"]["binding"]["session_id"]
    def at(timestamp):
        return dict(session_id=session, offset_ns=timestamp-origin)
    def consume(response):
        update = response.get("update", {})
        if update.get("rejections"):
            raise ValueError("RUST_ASSOCIATION_REJECTED")
        for pair in update.get("pairs", []):
            images = []
            for role in ("color", "depth"):
                frame = pair[role]
                stored = writer.token(frame["observation_sequence"], role, "image")
                info_id = frame.get("camera_info_observation_sequence")
                stored["camera_info"] = writer.token(info_id, role, "camera_info")
                metadata_id = frame.get("metadata_observation_sequence")
                if metadata_id is not None:
                    stored["metadata"] = writer.token(metadata_id, role, "metadata")
                images.append(stored)
            writer.record(dict(kind="observed-association", association=pair,
                               header_mapping=config["observer_config"]["streams"]["color"]["mapper"]),
                          images[0], images[1], representation)
    try:
        for topic, msg_type, timestamp, cdr, message in messages:
            if timestamp <= 0 or timestamp < last:
                raise ValueError("BAG_RECORD_TIME_REGRESSION")
            origin = timestamp if origin is None else origin
            last = timestamp
            number = writer.archive(topic, msg_type, timestamp, cdr)
            if backend == "rust" and topic == config["sdk_evidence_topic"]:
                # Keep publisher bytes unchanged in the wire archive; parse only to bind references.
                pair = strict_json(str(message.data))
                if pending_sdk is not None:
                    raise ValueError("SDK_PENDING_PAIR_LIMIT")
                original = writer.put(f"sdk-source-{number:020d}.json", str(message.data).encode())
                pending_sdk = (pair, original)
            else:
                role, kind = roles[topic]
                if kind == "image":
                    decoded = boundary.image(message)
                    stamp_ns = decoded["stamp"]["sec"] * 1_000_000_000 + decoded["stamp"]["nanosec"]
                    pixels = bytes(message.data)
                    if len(pixels) > MAX_MESSAGE:
                        raise ValueError("IMAGE_PAYLOAD_LIMIT")
                    descriptor = dict(file=writer.put(f"pixels-{number:020d}.raw", pixels), topic=topic,
                                      record_timestamp_ns=timestamp, header_timestamp_ns=stamp_ns,
                                      frame_id=decoded["frame_id"], width=decoded["width"], height=decoded["height"],
                                      stride=decoded["step"], encoding=decoded["encoding"],
                                      endian="big" if decoded["is_bigendian"] else "little", camera_info=None, metadata=None)
                    field, command = "image", "image"
                elif kind == "camera_info":
                    decoded = boundary.camera_info(message)
                    stamp_ns = decoded["stamp"]["sec"] * 1_000_000_000 + decoded["stamp"]["nanosec"]
                    descriptor = writer.put(f"info-{number:020d}.json", json_bytes(decoded))
                    field, command = "info", "camera-info"
                else:
                    decoded = boundary.metadata(message)
                    stamp_ns = decoded["stamp"]["sec"] * 1_000_000_000 + decoded["stamp"]["nanosec"]
                    descriptor = writer.put(f"metadata-{number:020d}.json", str(message.data).encode())
                    field, command = "metadata", "metadata"
                if engine:
                    request = dict(kind=command, stream=role, at=at(timestamp), **{field: decoded})
                    if kind == "image":
                        request["received_unix_ns"] = timestamp
                    response = engine.call(request)
                    token = response.get("update", {}).get("input_observation_sequence")
                    if token is None:
                        raise ValueError("RUST_OBSERVER_COMPONENT_PROTOCOL")
                    writer.component(token, role, kind, stamp_ns, descriptor)
                    consume(response)
                    consume(engine.call(dict(kind="tick", at=at(timestamp), received_unix_ns=timestamp)))
                else:
                    writer.component(number, role, kind, stamp_ns, descriptor)
            if backend == "rust" and pending_sdk is not None:
                pair, original = pending_sdk
                try:
                    color = writer.sdk_image("color", pair["color"]["context"]["system_timestamp_us"] * 1000)
                    depth = writer.sdk_image("depth", pair["depth"]["context"]["system_timestamp_us"] * 1000)
                except ValueError:
                    continue
                # Recorded SDK layout encoding uses depth16; ROS transports 16UC1.
                if depth["encoding"] == "16UC1":
                    depth["encoding"] = "depth16"
                writer.record(dict(kind="sdk-bound-pair", pair=pair, source_json=original), color, depth, representation)
                pending_sdk = None
        if origin is None or pending_sdk is not None:
            raise ValueError("BAG_EMPTY_OR_INCOMPLETE_PAIR")
        if engine:
            # EOF is a known offline watermark, not an invented live receipt/exposure time.
            consume(engine.call(dict(kind="watermark", at=at(last), unix_ns=2**64-1)))
            consume(engine.call(dict(kind="finalize")))
    finally:
        if engine:
            engine.close()


def run(args):
    config, _ = read_json(args.config)
    _, snapshot_raw = read_json(config["snapshot"])
    if config["backend"] not in ("official", "rust"):
        raise ValueError("BACKEND_INVALID")
    topics = {}
    for role in ("color", "depth"):
        for kind, topic in config["bindings"][role].items():
            if not isinstance(topic, str) or not topic.startswith("/") or topic in topics:
                raise ValueError("TOPIC_BINDING")
            topics[topic] = {"image":"sensor_msgs/msg/Image", "camera_info":"sensor_msgs/msg/CameraInfo", "metadata":"std_msgs/msg/String"}[kind]
    if config["backend"] == "official":
        observer = config["observer_config"]
        if observer["association"]["info_tolerance_ns"] != 0:
            raise ValueError("IMPORT_REQUIRES_EXACT_INFO_STAMP")
        if observer["streams"]["color"]["mapper"] != observer["streams"]["depth"]["mapper"]:
            raise ValueError("IMPORT_REQUIRES_COMMON_MAPPING")
    else:
        topic = config["sdk_evidence_topic"]
        if topic in topics or not topic.startswith("/"):
            raise ValueError("SDK_TOPIC_BINDING")
        topics[topic] = "std_msgs/msg/String"
    messages = BagReader(args.input, args.storage_id, topics)
    writer = RecordingWriter(args.output, snapshot_raw)
    try:
        import_messages(messages, config, snapshot_raw, writer, args.observer_engine)
        writer.finish(args.offline_engine, config["storage_representation"])
    except BaseException:
        writer.failed()
        raise
    print(json.dumps(dict(version=1, complete=True, records=writer.count, messages=writer.message_count)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--storage-id", choices=("sqlite3", "mcap"), required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--observer-engine", required=True)
    parser.add_argument("--offline-engine", required=True)
    args = parser.parse_args()
    try:
        run(args)
    except BaseException:
        print(json.dumps(dict(version=1, complete=False, code="RECORDING_IMPORT_FAILED")))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
