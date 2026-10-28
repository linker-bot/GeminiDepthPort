#!/usr/bin/env python3
"""Create an UNCONFIRMED private management intent from explicit SDK config.

Never opens hardware, never writes positive profile/calibration verification.
Use management_acceptance.py's actual preview/confirm APIs for confirmation.
"""
import argparse
import json
import os
from pathlib import Path
import re

REPO=Path(__file__).resolve().parents[2]
LIMITS=('idle_timeout_ms','startup_timeout_ms','heartbeat_ms','heartbeat_timeout_ms','max_restarts','restart_backoff_ms','shutdown_timeout_ms','ready_min_pairs')

def build(config,runtime,profile,image,min_fps=25,freshness=.5):
    if (config.get('version')!=1 or config.get('ros',{}).get('stamp_source')!='sdk-system-unix'
            or profile not in ('gemini_335l','gemini_305','gemini_2')
            or runtime.get('sdk_version')!='2.10.6'):
        raise ValueError('INPUT_CONTRACT')
    inventory=json.loads((REPO/'inventory.v2.example.json').read_text())
    if runtime.get('upstream_commit')!=inventory['upstream_commit']:raise ValueError('SDK_SOURCE_DIFFERS')
    for key in ('sdk_sha256','bridge_sha256','service_sha256'):
        if not re.fullmatch('[a-f0-9]{64}',runtime.get(key,'')):raise ValueError('RUNTIME_HASH_REQUIRED')
    color,depth=config['color'],config['depth']
    suffix='_color_optical_frame';frame=color['frame_id']
    if not frame.endswith(suffix):raise ValueError('EXPLICIT_FRAME_PREFIX_REQUIRED')
    prefix=frame[:-len(suffix)]
    if depth['frame_id']!=prefix+'_depth_optical_frame':raise ValueError('FRAME_BINDING_DIFFERS')
    if color['fps']!=depth['fps'] or not 0<min_fps<=color['fps']:raise ValueError('HEALTH_POLICY')
    inventory.update(capture_backend='rust',image=image)
    inventory['rust_capture']={'depth_unit_policy':depth['unit_policy'],'pairing':config['pairing'],
        'poll_ms':config['poll_ms'],'capacity':config['capacity'],
        'startup_discovery_seconds':config.get('startup_discovery_seconds',0),
        'service':{key:config['service'][key] for key in LIMITS}}
    for camera in inventory['cameras']:
        camera.update(enabled=False,profiles_verified=False,verification=None)
    camera=inventory['cameras'][0]
    camera.update(name='camera_a',enabled=True,profile=profile,serial_number=config['serial'],namespace=config['ros']['namespace'],frame_prefix=prefix,timestamp_source='host',depth_unit='mm',calibration_revision='runtime-observation-required',health={'discovery_seconds':3,'sample_seconds':3,'freshness_seconds':freshness,'min_fps_ratio':min_fps/color['fps']})
    for role in ('color','depth'):
        for key in ('width','height','fps'):camera['parameters'][role+'_'+key]=config[role][key]
    return inventory


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('capture-config','runtime','output'):p.add_argument('--'+flag,type=Path,required=True)
    p.add_argument('--profile',required=True);p.add_argument('--image',required=True)
    p.add_argument('--min-fps',type=float,default=25);p.add_argument('--freshness-seconds',type=float,default=.5)
    args=p.parse_args();os.umask(0o077)
    try:
        if not all(v.is_absolute() for v in (args.capture_config,args.runtime,args.output)) or args.output.exists():raise ValueError('PRIVATE_NEW_OUTPUT_REQUIRED')
        config=json.loads(args.capture_config.read_text());runtime=json.loads(args.runtime.read_text())
        inventory=build(config,runtime,args.profile,args.image,args.min_fps,args.freshness_seconds)
        descriptor=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as handle:json.dump(inventory,handle,sort_keys=True,allow_nan=False);handle.flush();os.fsync(handle.fileno())
        print(json.dumps({'ok':True,'version':1,'hardware_opened':False,'profiles_verified':False,'snapshot_fabricated':False,'calibration_fabricated':False,'source_only_policy_fields':[key for key in ('raw_recording','quality_runtime','quality_policy') if key in config]}));return 0
    except Exception:
        print(json.dumps({'ok':False,'errors':['MANAGEMENT_INTENT_INPUT_INVALID']}));return 1
if __name__=='__main__':raise SystemExit(main())
