#!/usr/bin/env python3
"""Actual synthetic ROS serialization/bag/CLI smoke; requires ROSbag2 plugins."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import bag_import as bag
from test_recording import fixture


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-id",choices=("sqlite3","mcap"),required=True)
    parser.add_argument("--observer-engine",required=True)
    parser.add_argument("--offline-engine",required=True)
    args=parser.parse_args()
    try:
        import rosbag2_py
        from rclpy.serialization import serialize_message
        from sensor_msgs.msg import Image, CameraInfo
        snapshot,config,messages=fixture()
        expected=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/"source-bag"
            writer=rosbag2_py.SequentialWriter()
            writer.open(rosbag2_py.StorageOptions(uri=str(source),storage_id=args.storage_id),
                        rosbag2_py.ConverterOptions("cdr","cdr"))
            for topic,kind,_,_,_ in messages:
                writer.create_topic(rosbag2_py.TopicMetadata(name=topic,type=kind,serialization_format="cdr"))
            for topic,kind,timestamp,_,decoded in messages:
                message=Image() if kind.endswith("/Image") else CameraInfo()
                message.header.stamp.sec=decoded.header.stamp.sec
                message.header.stamp.nanosec=decoded.header.stamp.nanosec
                message.header.frame_id=decoded.header.frame_id
                message.width=decoded.width;message.height=decoded.height
                if kind.endswith("/Image"):
                    message.encoding=decoded.encoding;message.step=decoded.step
                    message.is_bigendian=decoded.is_bigendian;message.data=decoded.data
                else:
                    message.distortion_model=decoded.distortion_model
                    message.k=decoded.k;message.d=decoded.d;message.r=decoded.r;message.p=decoded.p
                    message.binning_x=decoded.binning_x;message.binning_y=decoded.binning_y
                raw=serialize_message(message)
                writer.write(topic,raw,timestamp);expected.append(bytes(raw))
            del writer
            snapshot_path=root/"snapshot.json";snapshot_path.write_bytes(bag.json_bytes(snapshot))
            config["snapshot"]=str(snapshot_path)
            config_path=root/"config.json";config_path.write_bytes(bag.json_bytes(config))
            result=subprocess.run([sys.executable,str(Path(__file__).with_name("bag_import.py")),
                                   "--input",str(source),"--storage-id",args.storage_id,
                                   "--config",str(config_path),"--output",str(root/"imported"),
                                   "--observer-engine",args.observer_engine,"--offline-engine",args.offline_engine],
                                  stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=False)
            if result.returncode!=0:
                raise ValueError("IMPORT_FAILED")
            output=root/"imported"
            wire=[bag.strict_json(line) for line in (output/"wire.jsonl").read_bytes().splitlines()]
            if len(wire)!=len(expected) or any((output/entry["cdr"]["path"]).read_bytes()!=raw for entry,raw in zip(wire,expected)):
                raise ValueError("CDR_ROUNDTRIP_MISMATCH")
            records=(output/"records.jsonl").read_bytes().splitlines()
            if len(records)!=1 or not (output/"COMPLETE.json").is_file():
                raise ValueError("COMPLETION_MISMATCH")
        print(bag.json_bytes(dict(version=1,complete=True,actual_cdr=True,storage=args.storage_id,records=1)).decode())
        return 0
    except BaseException:
        print(bag.json_bytes(dict(version=1,complete=False,code="SYNTHETIC_BAG_SMOKE_FAILED")).decode())
        return 1


if __name__=="__main__":sys.exit(main())
