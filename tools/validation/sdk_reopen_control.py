#!/usr/bin/env python3
"""Explicit, test-only SDK SIGKILL/reopen control; never changes device properties."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def file_arg(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise argparse.ArgumentTypeError('absolute regular non-symlink file required')
    return path


def digest(path):
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
        return value.hexdigest()


def stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=3)


def run(args):
    if not args.execute:
        return {'version': 1, 'kind': 'sdk-reopen-plan', 'hardware_executed': False,
                'reopen_attempts': args.retries, 'initial_ready_timeout_seconds': 20,
                'reopen_timeout_seconds': 20, 'settle_seconds': 2}
    if not args.devices_released:
        raise ValueError('DEVICES_RELEASED_ACK_REQUIRED')
    target = args.output_dir
    if not target.is_absolute() or target.exists() or target.is_symlink():
        raise ValueError('NEW_ABSOLUTE_OUTPUT_REQUIRED')
    target.mkdir(mode=0o700)
    # Snapshot only the explicitly named config. No sibling files are copied.
    config = target / 'capture.json'
    config.write_bytes(args.config.read_bytes())
    config.chmod(0o600)
    report = {'version': 1, 'kind': 'sdk-sigkill-reopen-control',
              'scope': 'official-cpp-sdk-functional-control-not-production-recovery',
              'hardware_executed': True, 'physical_accuracy_verified': False,
              'config_sha256': digest(config), 'binary_sha256': digest(args.binary),
              'initial_ready': False, 'initial_sigkill': False, 'attempts': []}
    active = None
    try:
        with (target / 'initial.stdout').open('wb') as out, (target / 'initial.stderr').open('wb') as err:
            initial = target / 'initial'
            active = subprocess.Popen([str(args.binary), str(config), str(initial), '--hold-after-ready'],
                                      stdout=out, stderr=err, start_new_session=True)
            deadline = time.monotonic() + 20
            while not (initial / 'READY').is_file():
                if active.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('INITIAL_SDK_READY_FAILED')
                time.sleep(.05)
            if active.poll() is not None:
                raise RuntimeError('INITIAL_SDK_EXITED_BEFORE_INJECTION')
            report['initial_ready'] = True
            stop(active)
            report['initial_sigkill'] = active.returncode == -signal.SIGKILL
            active = None
        time.sleep(2)
        for index in range(1, args.retries + 1):
            attempt = target / f'reopen-{index}'
            with (target / f'reopen-{index}.stdout').open('wb') as out, (target / f'reopen-{index}.stderr').open('wb') as err:
                started = time.monotonic()
                active = subprocess.Popen([str(args.binary), str(config), str(attempt)],
                                          stdout=out, stderr=err, start_new_session=True)
                timed_out = False
                try:
                    active.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    stop(active)
                report['attempts'].append({'attempt': index, 'returncode': active.returncode,
                    'timed_out': timed_out, 'frameset_ready': (attempt / 'READY').is_file(),
                    'capture_complete': (attempt / 'CAPTURE_COMPLETE').is_file(),
                    'normal_exit': active.returncode == 0,
                    'elapsed_seconds': round(time.monotonic() - started, 3)})
                active = None
            # Each result remains distinct, including a failed first reopening.
        return report
    except Exception:
        report['error'] = 'CONTROL_INCOMPLETE'
        raise
    finally:
        if active is not None:
            stop(active)
        (target / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=file_arg)
    parser.add_argument('--binary', required=True, type=file_arg)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--retries', type=int, choices=(2, 3), default=2)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--devices-released', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = run(args)
    except Exception:
        print(json.dumps({'version': 1, 'ok': False, 'error': 'SDK_REOPEN_CONTROL_FAILED'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
