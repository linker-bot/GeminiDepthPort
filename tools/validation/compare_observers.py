#!/usr/bin/env python3
"""Compare like-for-like standard ROS consumer reports, never capture-only counts.

Each private run manifest must declare sdk_sha256, profile_sha256,
consumer_workload_sha256, platform_sha256, trial_id and implementation. Hashes
must be measured by the runner, not copied here to force comparability.
"""
import argparse
import json
from pathlib import Path
import re


def load(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('INPUT_SIZE')
    return json.loads(path.read_text())


def compare(baseline, candidate, baseline_manifest, candidate_manifest):
    hashes = ('sdk_sha256', 'profile_sha256', 'consumer_workload_sha256', 'platform_sha256')
    for field in hashes:
        if not isinstance(baseline_manifest.get(field), str) or not re.fullmatch('[a-f0-9]{64}', baseline_manifest[field]):
            raise ValueError('MANIFEST_INVALID')
        if baseline_manifest[field] != candidate_manifest.get(field):
            raise ValueError('INCOMPARABLE_RUNS')
    for report in (baseline, candidate):
        if report.get('version') != 1 or report.get('kind') != 'rgbd-consumer-observer' or report.get('mode') != 'standard-wire' or report.get('ok') is not True:
            raise ValueError('COMMON_ROS_CONSUMER_REQUIRED')
    for manifest in (baseline_manifest, candidate_manifest):
        if not isinstance(manifest.get('trial_id'), str) or not manifest['trial_id'] or not isinstance(manifest.get('implementation'), str):
            raise ValueError('MANIFEST_INVALID')
    streams = {}
    for role in ('color', 'depth'):
        b, c = baseline['streams'][role], candidate['streams'][role]
        delta = lambda x, y: y - x if type(x) in (int, float) and type(y) in (int, float) else None
        streams[role] = {
            'baseline': {key: b[key] for key in ('images', 'matched_frames', 'active_fps', 'image_gap_events', 'age_ms')},
            'candidate': {key: c[key] for key in ('images', 'matched_frames', 'active_fps', 'image_gap_events', 'age_ms')},
            'candidate_minus_baseline': {'active_fps': delta(b['active_fps'], c['active_fps']),
                'receive_age_mean_ms': delta(b['age_ms']['mean'], c['age_ms']['mean']),
                'receive_age_max_ms': delta(b['age_ms']['maximum'], c['age_ms']['maximum'])}}
    return {'version': 1, 'kind': 'paired-standard-ROS-consumer-comparison', 'ok': True,
            'same_sdk_profile_consumer_platform_asserted_by_run_manifests': True,
            'streams': streams, 'exposure_latency_compared': False,
            'cpu_memory_compared': False, 'statistical_significance_established': False,
            'scope': 'one-paired-run-descriptive-comparison-repeat-in-alternating-order-and-report-resource-measurements-separately'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'candidate', 'baseline-manifest', 'candidate-manifest'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare(load(args.baseline), load(args.candidate), load(args.baseline_manifest), load(args.candidate_manifest))
    except Exception as exc:
        code = str(exc) if type(exc) is ValueError and str(exc) in {
            'INPUT_SIZE', 'MANIFEST_INVALID', 'INCOMPARABLE_RUNS', 'COMMON_ROS_CONSUMER_REQUIRED'} else 'COMPARISON_FAILED'
        report = {'version': 1, 'kind': 'paired-standard-ROS-consumer-comparison', 'ok': False, 'errors': [code]}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__': raise SystemExit(main())
