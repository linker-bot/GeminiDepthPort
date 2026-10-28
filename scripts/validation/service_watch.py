#!/usr/bin/env python3
"""Bounded Linux service-status sampling, independent of ROS delivery checks.

Reads the container's actual tmpfs through /proc/PID/root. Run with permission to
read its root filesystem. Outputs anonymous indices and counters, never sessions,
PIDs, paths, calibration or device identities. Does not open SDK or send signals.
"""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import subprocess
import time


class Watch:
    def __init__(self, maximum_age_ms, maximum_pair_age_ms):
        self.maximum_age = maximum_age_ms
        self.maximum_pair_age = maximum_pair_age_ms
        self.states = Counter()
        self.errors = Counter()
        self.samples = 0
        self.session_changes = 0
        self.identity = None
        self.first = self.last = None
        self.maximum_observed_age = 0
        self.maximum_observed_pair_age = 0

    def observe(self, value, now_ms):
        self.samples += 1
        try:
            if not isinstance(value,dict) or value.get('version') != 1 or value.get('kind') != 'capture-service-status':
                raise ValueError('STATUS_SCHEMA')
            state = value['state']
            self.states[state if state in ('starting','ready','degraded','stopped') else 'invalid'] += 1
            if state != 'ready' or value.get('ready') is not True:
                self.errors['NOT_READY'] += 1
            identity = (value['session_id'], value['generation'], value['config_sha256'])
            if self.identity is not None and identity != self.identity:
                self.session_changes += 1
                self.errors['SESSION_OR_CONFIG_CHANGED'] += 1
            self.identity = identity
            age = now_ms - value['updated_unix_ms']
            pair_age = value['last_pair_age_ms'] + max(0, age)
            self.maximum_observed_age = max(self.maximum_observed_age, age)
            self.maximum_observed_pair_age = max(self.maximum_observed_pair_age, pair_age)
            if not -100 <= age <= self.maximum_age:
                self.errors['STALE_STATUS'] += 1
            if pair_age > self.maximum_pair_age:
                self.errors['STALE_PAIR'] += 1
            current = {k: value[k] for k in ('restart_attempt','pairs_published','pairs_rejected','publish_failures')}
            if not all(type(v) is int and v >= 0 for v in current.values()):
                raise ValueError('INVALID_COUNTER')
            if self.first is None:
                self.first = current
            self.last = current
        except (KeyError, TypeError, ValueError):
            self.errors['STATUS_INVALID'] += 1

    def summary(self):
        delta = {k: self.last[k] - self.first[k] for k in self.first} if self.first and self.last else {}
        errors = dict(self.errors)
        if not delta or any(v < 0 for v in delta.values()):
            errors['COUNTER_MISSING_OR_RESET'] = 1
        if delta.get('restart_attempt',0) or delta.get('publish_failures',0):
            errors['RESTART_OR_PUBLICATION_FAILURE'] = 1
        return {'ok': not errors, 'samples': self.samples, 'states': dict(self.states), 'errors': errors,
                'session_changes': self.session_changes, 'counter_deltas': delta,
                'max_status_age_ms': self.maximum_observed_age, 'max_pair_age_ms': self.maximum_observed_pair_age}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--container', action='append', required=True)
    p.add_argument('--seconds', type=float, required=True)
    p.add_argument('--interval', type=float, default=2)
    p.add_argument('--max-status-age-ms', type=int, required=True)
    p.add_argument('--max-pair-age-ms', type=int, required=True)
    p.add_argument('--summary', type=Path, required=True)
    a = p.parse_args()
    if not(math.isfinite(a.seconds) and 0 < a.seconds <= 86400 and math.isfinite(a.interval)
           and .1 <= a.interval <= 60 and a.seconds/a.interval <= 100000
           and 1 <= a.max_status_age_ms <= 120000 and 1 <= a.max_pair_age_ms <= 120000):
        p.error('invalid bounded limits')
    os.umask(0o077)
    reports, paths = [], []
    try:
        for name in a.container:
            result = subprocess.run(['docker','inspect','--format','{{.State.Pid}}',name],check=True,
                                    text=True,capture_output=True,timeout=10)
            pid = int(result.stdout.strip())
            if pid <= 0: raise ValueError('not running')
            paths.append(Path(f'/proc/{pid}/root/tmp/capture/status.json'))
            reports.append(Watch(a.max_status_age_ms,a.max_pair_age_ms))
        start = time.monotonic()
        while True:
            for path, watch in zip(paths,reports):
                try:
                    with path.open() as handle: raw = handle.read(65537)
                    if len(raw)>65536: raise ValueError('too large')
                    watch.observe(json.loads(raw),time.time_ns()//1_000_000)
                except (OSError,ValueError): watch.errors['STATUS_READ_FAILED'] += 1
            elapsed = time.monotonic()-start
            if elapsed >= a.seconds: break
            time.sleep(min(a.interval,a.seconds-elapsed))
        values = [r.summary() for r in reports]
        summary={'version':1,'kind':'resident-service-status-window','ok':all(r['ok'] for r in values),
                 'elapsed_seconds':elapsed,'scope':'sampled-status-not-ROS-delivery','results':values}
    except (OSError,ValueError,subprocess.SubprocessError):
        summary={'version':1,'kind':'resident-service-status-window','ok':False,'errors':['STATUS_MONITOR_FAILED']}
    a.summary.write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))
    return 0 if summary['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
