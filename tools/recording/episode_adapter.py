#!/usr/bin/env python3
"""Recording-context/quality evidence consumer; completion is not episode validity."""
import argparse
import json
import os
from pathlib import Path
import sys
try:
    from .bag_import import strict_json, read_json, json_bytes, digest
except ImportError:
    from bag_import import strict_json, read_json, json_bytes, digest


def capture(context_path, event_source, output, require_strong=False):
    context, raw = read_json(context_path)
    if context.get("version") != 1 or context.get("kind") != "recording-context":
        raise ValueError("CONTEXT_VERSION")
    protection = context["protection"]
    if require_strong and not (protection.get("strong_guarantee_available") is True
                              and protection.get("native_restart_gate") is True
                              and protection.get("session_bound") is True):
        raise ValueError("STRONG_PROTECTION_UNAVAILABLE")
    cameras = context["cameras"]
    if not 1 <= len(cameras) <= 16:
        raise ValueError("CONTEXT_CAMERA_LIMIT")
    for camera in cameras:
        snapshot = camera["snapshot"]
        binding = snapshot["content"]["binding"]
        if (binding["session_id"] != camera["session_id"]
                or binding["generation_id"] != context["generation_id"]
                or binding["instance_id"] != camera["instance_id"]):
            raise ValueError("CONTEXT_BINDING")
    output = Path(output)
    stage = output.with_name(output.name + ".partial")
    if output.exists() or stage.exists():
        raise ValueError("OUTPUT_EXISTS")
    stage.mkdir(mode=0o700)
    count = 0
    def save(name, value):
        fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as sink:
            sink.write(value)
            sink.flush()
            os.fsync(sink.fileno())
    save("recording-context.json", raw)
    try:
        fd = os.open(stage / "quality-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as sink:
            while True:
                line = event_source.readline(65537)
                if not line:
                    break
                if len(line) > 65536 or not line.endswith(b"\n"):
                    raise ValueError("EVENT_LINE_LIMIT_OR_TRUNCATION")
                value = strict_json(line)
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError("EVENT_ENVELOPE_VERSION")
                # Preserve EVENT_GAP, finalization, disconnect/session transitions
                # as source facts. They never become a business validity verdict.
                sink.write(line)
                count += 1
            sink.flush()
            os.fsync(sink.fileno())
        save("COMPLETE.json", json_bytes(dict(version=1, complete=True, context_sha256=digest(raw),
                                               event_envelopes=count, business_validity=None,
                                               physical_accuracy_verified=False)))
        stage.rename(output)
    except BaseException:
        save("INTERRUPTED.json", json_bytes(dict(version=1, complete=False, event_envelopes=count,
                                                  business_validity=None)))
        raise
    return dict(version=1, complete=True, event_envelopes=count, business_validity=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--events-jsonl", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-strong", action="store_true")
    args = parser.parse_args()
    try:
        with open(args.events_jsonl, "rb") as source:
            print(json.dumps(capture(args.context, source, args.output, args.require_strong)))
        return 0
    except BaseException:
        print(json.dumps(dict(version=1, complete=False, code="EPISODE_EVIDENCE_CAPTURE_FAILED")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
