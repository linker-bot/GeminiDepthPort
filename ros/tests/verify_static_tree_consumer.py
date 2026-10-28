#!/usr/bin/env python3
"""Synthetic tf2 static-tree consumer check: no device, SDK or ROS publisher."""
import json
import math
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import rclpy
from rclpy.time import Time
from tf2_ros import Buffer
from tf2_msgs.msg import TFMessage
from geometry_msgs.msg import TransformStamped
from runtime.rgbd_acceptance import StaticTfResolver


def transform(parent,child,translation,quaternion):
    edge=TransformStamped()
    edge.header.frame_id=parent;edge.child_frame_id=child
    edge.header.stamp.sec=1
    t,q=edge.transform.translation,edge.transform.rotation
    t.x,t.y,t.z=map(float,translation)
    q.x,q.y,q.z,q.w=map(float,quaternion)
    return edge


def near(actual,expected):
    assert len(actual)==len(expected) and all(abs(a-b)<=1e-9 for a,b in zip(actual,expected))


def main():
    rclpy.init()
    try:
        color,link,depth='synthetic_color_optical','synthetic_camera_link','synthetic_depth_optical'
        half=math.sqrt(.5)
        expected={'parent_frame':color,'child_frame':depth}
        buffer=Buffer()
        resolver=StaticTfResolver(buffer,expected,Time())
        # p_color = Rz(90)*p_link + (.1,.2,.3)
        first=transform(color,link,[.1,.2,.3],[0,0,half,half])
        # p_link = p_depth + (1,0,0), so composed translation is (.1,1.2,.3).
        second=transform(link,depth,[1,0,0],[0,0,0,1])
        assert resolver.feed(TFMessage(transforms=[first]))==[]
        projections=resolver.feed(TFMessage(transforms=[second]))
        assert len(projections)==1
        near(projections[0]['translation_m'],[.1,1.2,.3])
        near(projections[0]['rotation_xyzw'],[0,0,half,half])
        # An independent direct edge must resolve to exactly the same result.
        direct=StaticTfResolver(Buffer(),expected,Time())
        direct_projection=direct.feed(TFMessage(transforms=[transform(color,depth,[.1,1.2,.3],[0,0,half,half])]))
        near(projections[0]['translation_m'],direct_projection[0]['translation_m'])
        near(projections[0]['rotation_xyzw'],direct_projection[0]['rotation_xyzw'])
        # Lookup order must not silently invert the depth->color contract.
        inverse=buffer.lookup_transform(depth,color,Time())
        t,q=inverse.transform.translation,inverse.transform.rotation
        near([t.x,t.y,t.z],[-1.2,.1,-.3])
        near([q.x,q.y,q.z,q.w],[0,0,-half,half])
        assert StaticTfResolver(Buffer(),expected,Time()).feed(TFMessage())==[]
        print(json.dumps({'ok':True,'kind':'synthetic-static-tree-consumer',
                          'two_edge_composition_matches_direct':True,'lookup_direction_verified':True,
                          'missing_path_rejected':True,'sdk_accessed':False,'hardware_verified':False}))
    finally:
        rclpy.shutdown()


if __name__=='__main__':main()
