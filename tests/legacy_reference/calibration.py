"""Private calibration snapshots from published ROS evidence, never device SDK access.

Validation proves structure and binding, not physical calibration accuracy. Public
callers must use summary(), never print the private snapshot or raw exceptions.
"""
from functools import wraps
import hashlib
import json
import math
import re
from pathlib import Path

import configuration as config
from operation_guard import atomic_json

KIND = 'geminidepthport-calibration-v1'


def _contract(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except config.ConfigurationError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            raise config.ConfigurationError('CALIBRATION_INVALID', 'Malformed calibration evidence') from None
    return checked


def _numbers(value, size=None):
    config.require(isinstance(value, list) and (size is None or len(value) == size)
                   and all(type(x) in (int, float) and math.isfinite(x) for x in value),
                   'CALIBRATION_INVALID', 'Invalid calibration numeric array')
    return list(value)


@_contract
def capture_request(data, generation_id):
    """Build a private ROS observation request for the exact active configuration."""
    config.require(data.get('schema_version') == 2, 'CALIBRATION_SCHEMA', 'Explicit bindings required')
    config.validate_inventory(data, draft=True)
    config.require(isinstance(generation_id, str) and re.fullmatch('[0-9a-f]{32}', generation_id),
                   'CALIBRATION_GENERATION', 'Valid generation identifier required')
    cameras = []
    for camera in config.active(data):
        streams = {}
        for stream in ('color', 'depth'):
            p = camera['parameters']
            if p['enable_' + stream]:
                streams[stream] = {'topic': camera['namespace'] + '/' + stream + '/camera_info',
                                   'frame_id': camera['frame_prefix'] + '_' + stream + '_optical_frame',
                                   'width': p[stream + '_width'], 'height': p[stream + '_height']}
        cameras.append({'name': camera['name'], 'configuration_sha256': config.camera_digest(data, camera),
                        'calibration_revision': camera['calibration_revision'], 'streams': streams})
    config.require(bool(cameras), 'CALIBRATION_MISSING', 'No enabled cameras')
    return {'kind': KIND, 'generation': generation_id, 'cameras': cameras}


def _intrinsics(info, expected):
    config.require(isinstance(info, dict), 'CALIBRATION_MISSING', 'CameraInfo evidence required')
    config.require(info.get('frame_id') == expected['frame_id']
                   and info.get('width') == expected['width'] and info.get('height') == expected['height'],
                   'CALIBRATION_BINDING', 'CameraInfo geometry or optical frame differs')
    k, r, p = (_numbers(info.get(key), size) for key, size in (('k', 9), ('r', 9), ('p', 12)))
    d = _numbers(info.get('d'))
    identity = [1., 0., 0., 0., 1., 0., 0., 0., 1.]
    gram = [sum(r[row*3+j] * r[col*3+j] for j in range(3)) for row in range(3) for col in range(3)]
    determinant = r[0]*(r[4]*r[8]-r[5]*r[7]) - r[1]*(r[3]*r[8]-r[5]*r[6]) + r[2]*(r[3]*r[7]-r[4]*r[6])
    config.require(all(abs(a-b) < 1e-5 for a,b in zip(gram, identity)) and abs(determinant-1) < 1e-5
                   and p[0] > 0 and p[5] > 0 and abs(p[10]-1) < 1e-6
                   and all(abs(p[i]) < 1e-6 for i in (8, 9, 11)),
                   'CALIBRATION_GEOMETRY', 'Valid rectification and projection matrices required')
    config.require(k[0] > 0 and k[4] > 0 and abs(k[8] - 1) < 1e-6
                   and abs(k[6]) < 1e-6 and abs(k[7]) < 1e-6,
                   'CALIBRATION_UNCALIBRATED', 'Valid focal lengths and homogeneous intrinsics required')
    distortion_lengths = {'plumb_bob': 5, 'rational_polynomial': 8, 'equidistant': 4}
    model = info.get('distortion_model')
    config.require(isinstance(model, str) and model in distortion_lengths,
                   'CALIBRATION_DISTORTION', 'Unsupported distortion model')
    config.require(len(d) == distortion_lengths[model],
                   'CALIBRATION_DISTORTION', 'Distortion coefficients do not match model')
    # This interface exports native full-frame geometry, never silently corrected ROI/binning.
    config.require(info.get('binning_x', 0) in (0, 1) and info.get('binning_y', 0) in (0, 1)
                   and not any(info.get('roi', {}).get(key, 0) for key in ('x_offset', 'y_offset', 'height', 'width')),
                   'CALIBRATION_GEOMETRY', 'Binned or cropped calibration needs an explicit geometry adapter')
    return dict(frame_id=expected['frame_id'], width=expected['width'], height=expected['height'],
                distortion_model=info['distortion_model'], k=k, d=d, r=r, p=p)


def _extrinsics(value, streams):
    config.require(isinstance(value, dict), 'CALIBRATION_MISSING', 'Depth-to-color transform required')
    config.require(value.get('source_frame') == streams['depth']['frame_id']
                   and value.get('target_frame') == streams['color']['frame_id']
                   and value.get('translation_unit') == 'm'
                   and value.get('rotation_representation') == 'quaternion_xyzw',
                   'CALIBRATION_TRANSFORM', 'Explicit depth-to-color direction, meters and xyzw required')
    translation = _numbers(value.get('translation'), 3)
    rotation = _numbers(value.get('rotation'), 4)
    config.require(abs(sum(x*x for x in rotation) - 1) <= 1e-5,
                   'CALIBRATION_TRANSFORM', 'Unit quaternion required')
    return {key: value[key] for key in ('source_frame', 'target_frame', 'translation_unit', 'rotation_representation')} | {
        'translation': translation, 'rotation': rotation,
        'equation': 'p_target_m = R_source_to_target * p_source_m + translation_m'}


@_contract
def build_snapshot(data, generation_id, observations):
    request = capture_request(data, generation_id)
    config.require(isinstance(observations, dict) and observations.get('request_sha256') == request_digest(request),
                   'CALIBRATION_BINDING', 'Observation request differs from active configuration')
    config.require(observations.get('source') in ('ros-camera-info-tf', 'offline-evidence'),
                   'CALIBRATION_SOURCE', 'Explicit evidence source required')
    observed = observations.get('cameras')
    config.require(isinstance(observed, dict) and set(observed) == {c['name'] for c in request['cameras']},
                   'CALIBRATION_MISSING', 'All enabled cameras require evidence')
    cameras = []
    for camera in request['cameras']:
        evidence = observed[camera['name']]
        config.require(isinstance(evidence, dict), 'CALIBRATION_INVALID', 'Invalid camera evidence')
        streams = camera['streams']
        intrinsics = {stream: _intrinsics(evidence.get(stream), expected) for stream, expected in streams.items()}
        item = dict(camera, intrinsics=intrinsics)
        if set(streams) == {'color', 'depth'}:
            item['depth_to_color'] = _extrinsics(evidence.get('depth_to_color'), streams)
        cameras.append(item)
    payload = dict(kind=KIND, generation=generation_id, request_sha256=request_digest(request), cameras=cameras,
                   evidence_source=observations['source'], structural_validation=True,
                   physical_accuracy_verified=False, robot_hand_eye_included=False)
    return dict(payload, snapshot_sha256=request_digest(payload))


def request_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@_contract
def validate_snapshot(snapshot, data, generation_id):
    config.require(isinstance(snapshot, dict), 'CALIBRATION_INVALID', 'Invalid calibration snapshot')
    payload = {k: v for k, v in snapshot.items() if k != 'snapshot_sha256'}
    config.require(snapshot.get('snapshot_sha256') == request_digest(payload)
                   and snapshot.get('request_sha256') == request_digest(capture_request(data, generation_id)),
                   'CALIBRATION_BINDING', 'Calibration content or configuration changed')
    # Revalidate full contents; a caller can recompute a digest and it is not authentication.
    observations = {'source': snapshot.get('evidence_source'), 'request_sha256': snapshot['request_sha256'],
                    'cameras': {c['name']: dict(c['intrinsics'], **({'depth_to_color': c['depth_to_color']}
                               if 'depth_to_color' in c else {})) for c in snapshot['cameras']}}
    config.require(build_snapshot(data, generation_id, observations) == snapshot,
                   'CALIBRATION_INVALID', 'Invalid calibration snapshot fields')
    return snapshot


def save_snapshot(root, snapshot, data, generation_id):
    validate_snapshot(snapshot, data, generation_id)
    path = Path(root) / 'state/calibration' / (snapshot['snapshot_sha256'] + '.json')
    atomic_json(path, snapshot)
    return summary(snapshot)


def summary(snapshot):
    """Allowlisted response: excludes device identity, frames, topics and calibration."""
    return {'kind': KIND, 'camera_count': len(snapshot['cameras']),
            'structural_validation': True, 'physical_accuracy_verified': False,
            'snapshot_sha256': snapshot['snapshot_sha256']}
