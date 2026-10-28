#!/usr/bin/env python3
"""Measure cgroup-v2 containers over a declared window; no pixel/config collection.

Output contains anonymous container indices, not names, PIDs or host paths.
CPU cores = delta cpu.stat usage_usec / elapsed microseconds. Memory is cgroup
memory.current (includes cache), not summed RSS or PSS. Monitoring has overhead.
"""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import time


def counter_map(text):
    return {key: int(value) for key, value in (line.split() for line in text.splitlines())}


def read_group(root):
    cpu = counter_map((root / 'cpu.stat').read_text())
    events = counter_map((root / 'memory.events').read_text())
    return {'cpu_usec': cpu['usage_usec'], 'memory_bytes': int((root / 'memory.current').read_text()),
            'oom_kill': events['oom_kill'],
            'memory_peak_bytes': int((root / 'memory.peak').read_text()) if (root / 'memory.peak').exists() else None}


def locate(name):
    result = subprocess.run(['docker', 'inspect', '--format', '{{.State.Pid}}', name],
                            check=True, capture_output=True, text=True, timeout=10)
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise ValueError('container not running')
    entries = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    entry = next(line[3:] for line in entries if line.startswith('0::'))
    root = Path('/sys/fs/cgroup') / entry.lstrip('/')
    if not root.is_relative_to(Path('/sys/fs/cgroup')):
        raise ValueError('invalid cgroup')
    return root


def summarize(samples, elapsed, expected):
    if elapsed <= 0 or len(samples) < 2:
        raise ValueError('invalid sampling window')
    results = []
    for i in range(expected):
        rows = [s['groups'][i] for s in samples]
        if rows[-1]['cpu_usec'] < rows[0]['cpu_usec'] or rows[-1]['oom_kill'] < rows[0]['oom_kill']:
            raise ValueError('cgroup counter reset')
        memory = [r['memory_bytes'] for r in rows]
        results.append({'index': i, 'cpu_cores': (rows[-1]['cpu_usec'] - rows[0]['cpu_usec']) / 1e6 / elapsed,
                        'memory_first_bytes': memory[0], 'memory_last_bytes': memory[-1],
                        'memory_sample_peak_bytes': max(memory),
                        'memory_cgroup_lifetime_peak_bytes': rows[-1]['memory_peak_bytes'],
                        'memory_first_quartile_mean_bytes': sum(memory[:max(1,len(memory)//4)]) / max(1,len(memory)//4),
                        'memory_last_quartile_mean_bytes': sum(memory[-max(1,len(memory)//4):]) / max(1,len(memory)//4),
                        'oom_kills': rows[-1]['oom_kill'] - rows[0]['oom_kill']})
    return {'version': 1, 'kind': 'cgroup-resource-window', 'ok': all(r['oom_kills'] == 0 for r in results),
            'elapsed_seconds': elapsed, 'samples': len(samples), 'scope': 'selected-container-cgroups-only-not-whole-host',
            'cpu_unit': 'logical-core-equivalents', 'memory_unit': 'bytes-cgroup-including-cache',
            'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', action='append', required=True)
    parser.add_argument('--seconds', type=float, required=True)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--summary', type=Path, required=True)
    args = parser.parse_args()
    if not (math.isfinite(args.seconds) and 0 < args.seconds <= 86400 and math.isfinite(args.interval)
            and .1 <= args.interval <= 60 and args.seconds / args.interval <= 100000):
        parser.error('invalid bounded window')
    os.umask(0o077)
    try:
        roots = [locate(name) for name in args.container]
        start = time.monotonic()
        samples = []
        while True:
            samples.append({'groups': [read_group(root) for root in roots]})
            elapsed = time.monotonic() - start
            if elapsed >= args.seconds:
                break
            time.sleep(min(args.interval, args.seconds - elapsed))
        report = summarize(samples, elapsed, len(roots))
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        report = {'version': 1, 'kind': 'cgroup-resource-window', 'ok': False, 'error': 'RESOURCE_SAMPLING_FAILED'}
    args.summary.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
