import copy
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

import bag_import as bag
import episode_adapter

ROOT = Path(__file__).resolve().parents[2]
BINS = Path(os.environ.get("GDP_RECORDING_BIN_DIR", ROOT / "target/debug"))


def content_id(kind, content):
    # Synthetic-only fixture builder. Acceptance is independently done by Rust.
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return bag.digest(b"geminidepthport:capture-contract:v1:" + kind.encode() + b"\0" + raw)


def fixture():
    session = "a" * 32
    instance = "b" * 64
    generation = "c" * 32
    model = lambda role: dict(frame_id=role, width=2, height=1, k=[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0],
                              distortion_model="none", distortion_coefficients=[])
    calibration = dict(version=1, source="ros-camera-info-tf", actual_runtime_observed=True,
                       source_session_id=session, source_instance_id=instance, observed_unix_ns=1,
                       models={role:model(role) for role in ("color","depth")},
                       transforms=[dict(source_frame="depth", target_frame="color",
                                        rotation_row_major=[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0], translation_m=[0.0]*3)],
                       physical_accuracy_verified=False, legacy_source_sha256=None)
    configuration = {"synthetic":True}
    binding = dict(instance_id=instance,session_id=session,generation_id=generation,
                   configuration_id=content_id("configuration",configuration), calibration_id=content_id("calibration",calibration))
    quality = dict(version=1,max_source_age_ns=500000000,max_future_ns=100000000,max_pair_delta_us=20000,
                   min_valid_fraction=0.0,max_zero_fraction=1.0,action="report-only")
    representation = dict(representation="millimetres",quantization="nearest-half-up-preserve-zero-clamp-positive-reject-overflow")
    content = dict(version=1,binding=binding,configuration=configuration,calibration=calibration,
                   depth=representation,quality_policy=quality)
    snapshot = dict(version=1,snapshot_id=content_id("snapshot",content),content=content)
    violations = ["STREAM_STALE","TIMESTAMP_REGRESSION","PAIR_SKEW_EXCEEDED","FRAME_CONTRACT_INVALID",
                  "QUEUE_OVERFLOW","DEVICE_DISCONNECTED","SESSION_CHANGED","PUBLISH_PARTIAL_FAILURE","TIME_MAPPING_INVALID"]
    observer = dict(version=1,streams={},association=dict(queue_capacity=8,info_tolerance_ns=0,metadata_tolerance_ns=0,
                    pair_tolerance_ns=20000000,watermark_lateness_ns=100000000,max_payload_bytes=1024),
                    quality=dict(version=1,quality=quality,stale_after_ns=500000000,event_capacity=16,window_capacity=8,
                                 window_horizon_ns=1000000000,summary_capacity=2,max_sessions=4,
                                 rules={v:dict(open_after=1,recover_after=2,severity="degraded") for v in violations}))
    bindings = {}
    for role in ("color","depth"):
        context = dict(version=1,session_id=session,generation_id=generation,configuration_id=binding["configuration_id"],
                       calibration_id=binding["calibration_id"],snapshot_id=snapshot["snapshot_id"],stream_id=role,
                       sequence=None,device_timestamp_us=None,system_timestamp_us=None,received_mono_offset_ns=0)
        observer["streams"][role] = dict(context=context,encoding="yuyv" if role=="color" else "16UC1",camera=model(role),
            rectification=[1.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,1.0],projection=[1.0,0.0,0.0,0.0,0.0,1.0,0.0,0.0,0.0,0.0,1.0,0.0],
            camera_info_tolerance=1e-9,mapper={"kind":"declared-observed-unix-header"},metadata_required=False)
        bindings[role] = dict(image=f"/synthetic/{role}/image",camera_info=f"/synthetic/{role}/info")
    config = dict(backend="official",observer_config=observer,bindings=bindings,storage_representation=representation)
    messages = []
    for role in ("color","depth"):
        header = N(stamp=N(sec=1,nanosec=0),frame_id=role)
        info = N(header=header,width=2,height=1,distortion_model="",k=model(role)["k"],d=[],
                 r=observer["streams"][role]["rectification"],p=observer["streams"][role]["projection"],
                 binning_x=0,binning_y=0,roi=N(x_offset=0,y_offset=0,width=0,height=0,do_rectify=False))
        image = N(header=header,width=2,height=1,step=4,encoding="yuyv" if role=="color" else "16UC1",
                  is_bigendian=0,data=b"\x01\x02\x03\x04" if role=="color" else struct.pack("<HH",1234,0))
        messages += [(bindings[role]["camera_info"],"sensor_msgs/msg/CameraInfo",1000000010,b"synthetic-cdr-info-"+role.encode(),info),
                     (bindings[role]["image"],"sensor_msgs/msg/Image",1000000011,b"synthetic-cdr-image-"+role.encode(),image)]
    # Keep record receipt timestamps monotonic, without changing source headers.
    messages = [(t,k,1000000010+i,c,m) for i,(t,k,_,c,m) in enumerate(messages)]
    return snapshot, config, messages


class BoundaryTests(unittest.TestCase):
    def test_bag_reader_preserves_cdr_bytes_and_exact_type_binding(self):
        observed = []
        class Reader:
            def open(self,*_): pass
            def get_all_topics_and_types(self): return [N(name="/synthetic",type="std_msgs/msg/String")]
            def set_filter(self, value): observed.append(value)
            def has_next(self): return not observed[-1].get("done",False)
            def read_next(self): observed[-1]["done"]=True;return "/synthetic", b"\x00\x01\x00\x00raw", 9
        rosbag = N(SequentialReader=Reader,StorageOptions=lambda **k:k,ConverterOptions=lambda *a:a,
                   StorageFilter=lambda **k:k)
        serialization = N(deserialize_message=lambda raw, cls: (raw,cls))
        utilities = N(get_message=lambda kind:kind)
        with patch.dict(sys.modules,{"rosbag2_py":rosbag,"rclpy.serialization":serialization,"rosidl_runtime_py.utilities":utilities}):
            result = list(bag.BagReader("synthetic", "mcap", {"/synthetic":"std_msgs/msg/String"}))
        self.assertEqual(result[0][3],b"\x00\x01\x00\x00raw")
        self.assertEqual(result[0][2],9)

    def test_config_and_episode_records_are_strict_and_do_not_decide_validity(self):
        with self.assertRaises(ValueError): bag.strict_json('{"a":{"b":1,"b":2}}')
        with self.assertRaises(ValueError): bag.strict_json('{"a":NaN}')
        with self.assertRaises(ValueError): bag.strict_json('{"a":1e999}')
        snapshot,_,_=fixture()
        context=dict(version=1,kind="recording-context",backend="official",generation_id=snapshot["content"]["binding"]["generation_id"],
                     cameras=[dict(snapshot=snapshot,session_id=snapshot["content"]["binding"]["session_id"],
                                   instance_id=snapshot["content"]["binding"]["instance_id"])],
                     protection=dict(strong_guarantee_available=False,native_restart_gate=False,session_bound=False))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/"context.json";source.write_bytes(bag.json_bytes(context))
            with self.assertRaises(ValueError): episode_adapter.capture(source,io.BytesIO(),root/"strong",True)
            raw=b'{"version":1,"gap":{"code":"EVENT_GAP"}}\n'
            result=episode_adapter.capture(source,io.BytesIO(raw),root/"facts")
            self.assertIsNone(result["business_validity"])
            self.assertEqual((root/"facts/quality-events.jsonl").read_bytes(),raw)
            with self.assertRaises(ValueError): episode_adapter.capture(source,io.BytesIO(b'{"version":1}'),root/"interrupted")
            self.assertTrue((root/"interrupted.partial/INTERRUPTED.json").exists())

    @unittest.skipUnless((BINS/"capture-observer").exists(), "build Rust observer CLI first")
    def test_rust_ambiguity_rejection_keeps_only_partial_recording(self):
        snapshot,config,messages=fixture()
        # Duplicate color CameraInfo is deliberately ambiguous; Python must not
        # pick the first Info or silently omit the failed component.
        messages.insert(1,messages[0])
        messages=[(t,k,1000000010+i,c,m) for i,(t,k,_,c,m) in enumerate(messages)]
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"ambiguous"
            writer=bag.RecordingWriter(output,bag.json_bytes(snapshot))
            with self.assertRaisesRegex(ValueError,"RUST_ASSOCIATION_REJECTED"):
                try:
                    bag.import_messages(messages,config,bag.json_bytes(snapshot),writer,str(BINS/"capture-observer"))
                except BaseException:
                    writer.failed();raise
            self.assertFalse(output.exists())
            self.assertTrue(output.with_name("ambiguous.partial").joinpath("FAILED.json").exists())
            self.assertFalse(output.with_name("ambiguous.partial").joinpath("COMPLETE.json").exists())

    @unittest.skipUnless((BINS/"offline-depth").exists(), "build Rust CLI first")
    def test_sdk_branch_uses_recorded_pair_and_exact_reference_lookup(self):
        snapshot, config, messages = fixture()
        config["backend"]="rust"
        config["sdk_evidence_topic"]="/synthetic/sdk_pair"
        frame = lambda role: dict(context={**config["observer_config"]["streams"][role]["context"],
            "sequence":17 if role=="color" else 31,"device_timestamp_us":1000,"system_timestamp_us":1000000},
            encoding="yuyv" if role=="color" else "depth16",width=2,height=1,stride=4,
            source_depth_scale_mm=None if role=="color" else 1.0,
            output_depth_scale_mm=None if role=="color" else 1.0,normalized=False)
        pair=dict(version=1,pair_sequence=1,color=frame("color"),depth=frame("depth"),
                  color_stamp_us=1000,depth_stamp_us=1000,clock_source="device_us",delta_us=0,
                  association="sdk_frameset",hardware_sync_verified=False)
        original=json.dumps(pair,indent=2)
        messages.append((config["sdk_evidence_topic"],"std_msgs/msg/String",1000000014,b"synthetic-original-sdk-cdr",N(data=original)))
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/"sdk"
            writer=bag.RecordingWriter(output,bag.json_bytes(snapshot))
            try:
                bag.import_messages(messages,config,bag.json_bytes(snapshot),writer,"unused-no-observer")
                writer.finish(str(BINS/"offline-depth"),config["storage_representation"])
            except BaseException:
                writer.failed();raise
            record=bag.strict_json((output/"records.jsonl").read_bytes())
            tagged=bag.strict_json((output/record["evidence"]["path"]).read_bytes())
            self.assertEqual(tagged["pair"]["depth"]["context"]["sequence"],31)
            self.assertEqual((output/tagged["source_json"]["path"]).read_text(),original)

    @unittest.skipUnless((BINS/"capture-observer").exists() and (BINS/"offline-depth").exists(), "build Rust CLIs first")
    def test_real_rust_association_and_offline_cli_keep_original_evidence(self):
        snapshot, config, messages = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/"imported"
            writer=bag.RecordingWriter(output,bag.json_bytes(snapshot))
            try:
                bag.import_messages(messages,config,bag.json_bytes(snapshot),writer,str(BINS/"capture-observer"))
                writer.finish(str(BINS/"offline-depth"),config["storage_representation"])
            except BaseException:
                writer.failed();raise
            self.assertEqual(writer.count,1)
            records=[bag.strict_json(line) for line in (output/"records.jsonl").read_bytes().splitlines()]
            evidence=bag.strict_json((output/records[0]["evidence"]["path"]).read_bytes())
            self.assertEqual(evidence["kind"],"observed-association")
            self.assertIsNone(evidence["association"]["same_sdk_frameset"])
            self.assertIsNone(evidence["association"]["depth"]["context"]["sequence"])
            self.assertEqual((output/records[0]["depth"]["file"]["path"]).read_bytes(),struct.pack("<HH",1234,0))
            wire=[bag.strict_json(line) for line in (output/"wire.jsonl").read_bytes().splitlines()]
            self.assertEqual((output/wire[0]["cdr"]["path"]).read_bytes(),messages[0][3])
            self.assertEqual(wire[0]["topic"],messages[0][0])
            self.assertTrue((output/"COMPLETE.json").exists())


if __name__ == "__main__": unittest.main()
