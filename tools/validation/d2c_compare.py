#!/usr/bin/env python3
"""Compare scalar color-Z depth with SDK Align on one retained actual frameset.

Differences are measurements, not an oracle pass/fail or physical certification.
"""
import argparse
from array import array
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys

MAX_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024


def require(ok, code):
    if not ok:
        raise ValueError(code)


def read(path, limit=MAX_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'COMPARISON_INPUT_LIMIT')
        value = source.read(limit + 1)
    require(len(value) <= limit, 'COMPARISON_INPUT_LIMIT')
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def strict(raw):
    def invalid(_):
        raise ValueError('NONFINITE_JSON')
    def finite_float(raw):
        value=float(raw)
        require(math.isfinite(value), 'NONFINITE_JSON')
        return value
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid, parse_float=finite_float)


def samples(raw, count):
    require(len(raw) == count * 2, 'COMPARISON_OUTPUT_LAYOUT')
    values = array('H')
    values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    return values


def metrics(scalar, sdk, sdk_scale):
    require(len(scalar) == len(sdk) and type(sdk_scale) in (int, float) and math.isfinite(sdk_scale) and sdk_scale > 0, 'COMPARISON_UNITS')
    common, scalar_only, sdk_only, exact, sdk_overflow = 0, 0, 0, 0, 0
    differences = []
    for ours, theirs in zip(scalar, sdk):
        scaled=theirs*sdk_scale
        if not math.isfinite(scaled) or scaled >= 65535.5:
            sdk_overflow += 1
            continue
        sdk_mm = 0 if theirs == 0 else max(1, math.floor(scaled + .5))
        if sdk_mm > 65535:
            sdk_overflow += 1
            continue
        if ours and sdk_mm:
            common += 1
            delta = abs(ours - sdk_mm)
            differences.append(delta)
            exact += delta == 0
        elif ours:
            scalar_only += 1
        elif sdk_mm:
            sdk_only += 1
    differences.sort()
    def percentile(p):
        return differences[min(len(differences)-1, max(0, math.ceil(len(differences)*p)-1))] if differences else None
    return dict(scalar_valid=sum(v != 0 for v in scalar), sdk_nonzero=sum(v != 0 for v in sdk),
                common_valid=common, scalar_only=scalar_only, sdk_only=sdk_only, sdk_mm_overflow=sdk_overflow,
                common_exact_equal=exact, common_abs_difference_mm=dict(
                    mean=sum(differences)/len(differences) if differences else None,
                    p50=percentile(.5), p95=percentile(.95), maximum=differences[-1] if differences else None),
                sdk_mm_conversion='half-up-preserve-zero-clamp-positive-reject-overflow',
                sdk_mm_conversion_complete=sdk_overflow == 0,
                scalar_z_semantics='color-camera-Z', sdk_z_semantics='SDK-reported-not-assumed-equivalent')


def compare(directory, scalar_program):
    directory = Path(directory).resolve(strict=True)
    require((directory/'CAPTURE_COMPLETE').is_file(), 'SDK_CAPTURE_INCOMPLETE')
    require(Path(scalar_program).is_absolute(), 'ABSOLUTE_SCALAR_PROGRAM_REQUIRED')
    descriptor_raw = read(directory/'descriptor.json', 65536)
    descriptor = strict(descriptor_raw)
    require(descriptor['version'] == 1 and descriptor['association'] == 'sdk_frameset'
            and descriptor['hardware_sync_verified'] is False, 'SDK_CAPTURE_PROVENANCE')
    require(descriptor['depth_endian'] == 'little', 'SDK_CAPTURE_ENDIAN')
    require(descriptor.get('selected_clock') in ('device','system')
            and all(type(descriptor.get(k)) is int and descriptor[k] > 0 for k in ('selected_color_timestamp_us','selected_depth_timestamp_us','max_pair_delta_us'))
            and abs(descriptor['selected_color_timestamp_us']-descriptor['selected_depth_timestamp_us']) <= descriptor['max_pair_delta_us'], 'SDK_PAIR_SELECTION_INVALID')
    require(descriptor.get('target_grid') == 'sdk-aligned-output-profile'
            and descriptor.get('target_transform_source') == 'depth-profile-to-sdk-aligned-profile', 'EXPLICIT_SDK_TARGET_GRID_REQUIRED')
    color, depth, target = descriptor['color'], descriptor['depth'], descriptor['target']
    pixels = target['width'] * target['height']
    require(type(pixels) is int and 0 < pixels <= MAX_PIXELS, 'COMPARISON_PIXEL_LIMIT')
    require(descriptor['depth_stride'] == depth['width']*2, 'COMPARISON_DEPTH_STRIDE')
    inputs = {name: read(directory/name) for name in ('depth.raw16', 'color.yuyv', 'sdk-aligned.raw16')}
    require(len(inputs['depth.raw16']) == descriptor['depth_stride']*depth['height']
            and len(inputs['color.yuyv']) == color['width']*color['height']*2, 'SDK_CAPTURE_LAYOUT')
    sdk = samples(inputs['sdk-aligned.raw16'], pixels)
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in inputs.items()}
    hashes['descriptor.json'] = hashlib.sha256(descriptor_raw).hexdigest()
    output = directory/'scalar-color-z.raw16'
    require(not output.exists() and not output.is_symlink(), 'NEW_SCALAR_OUTPUT_REQUIRED')
    process = subprocess.run([str(scalar_program), str(directory/'descriptor.json'), str(directory/'depth.raw16'), str(output)],
                             capture_output=True, timeout=60)
    require(len(process.stdout) <= 65536, 'SCALAR_RESPONSE_LIMIT')
    scalar_result = strict(process.stdout)
    require(all(hashlib.sha256(read(directory/name)).hexdigest() == expected for name, expected in hashes.items()), 'SOURCE_CHANGED_DURING_COMPARISON')
    result = dict(version=1, scope='SDK-algorithm-difference-not-physical-or-bitwise-certification',
                  hardware_sync_verified=False, physical_accuracy_verified=False,
                  target_grid='sdk-aligned-output-profile',
                  target_transform_source='depth-profile-to-sdk-aligned-profile',
                  sdk_rasterization='SDK-Align-including-implementation-gap-fill-policy',
                  scalar_rasterization='nearest-pixel-color-Z-zbuffer-no-gap-fill',
                  selected_pair=dict(color_sequence=descriptor['color_sequence'], depth_sequence=descriptor['depth_sequence'],
                                     clock=descriptor['selected_clock'],
                                     delta_us=abs(descriptor['selected_color_timestamp_us']-descriptor['selected_depth_timestamp_us'])),
                  input_sha256=hashes, scalar_result=scalar_result, complete=process.returncode == 0)
    if process.returncode == 0:
        require(scalar_result.get('complete') is True and scalar_result.get('target_grid') == 'sdk-aligned-output-profile', 'SCALAR_RESULT_MISMATCH')
        result['differences'] = metrics(samples(read(output), pixels), sdk, descriptor['sdk_aligned_scale_mm'])
        if not result['differences']['sdk_mm_conversion_complete']:
            result['complete'] = False
            result['code'] = 'SDK_MM_CONVERSION_OVERFLOW'
    else:
        require(scalar_result.get('complete') is False, 'SCALAR_RESULT_MISMATCH')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--scalar-program', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(args.input, args.scalar_program)
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(result, out, sort_keys=True, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        print(json.dumps({'version': 1, 'complete': result['complete'], 'scope': result['scope']}))
        return 0 if result['complete'] else 1
    except Exception:
        print(json.dumps({'version': 1, 'complete': False, 'code': 'SDK_COMPARISON_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
