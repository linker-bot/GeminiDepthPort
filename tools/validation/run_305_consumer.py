#!/usr/bin/env python3
"""Target-host runner for explicitly authorized private camera service tests.

Run on the Linux target, not via this script's own SSH. Never opens the SDK,
reads camera identity, changes the service, or operates other camera containers.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess


def call(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('CONTROL_COMMAND_FAILED')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('short', 'replay', 'bag', 'long', 'status'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--camera-tag', choices=('305', '335L'), default='305')
    parser.add_argument('--image', required=True)
    parser.add_argument('--domain', type=int, required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0 or not args.root.is_absolute() or not 0 <= args.domain <= 232:
            raise ValueError('RUNNER_ARGUMENT')
        tag = args.camera_tag
        private = args.root/'private'/tag
        inside = '/test/private/'+tag+'/'
        if args.stage == 'status':
            states = {}
            for name in ('gdp-consumer-'+tag+'-preflight', 'gdp-consumer-'+tag+'-bag', 'gdp-consumer-'+tag):
                result = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True)
                if result.returncode == 0:
                    state = json.loads(result.stdout)[0]['State']
                    states[name] = {'status': state['Status'], 'exit_code': state['ExitCode']}
            reports = {}
            for name in ('observer-short-summary.json', 'observer-short-replay-summary.json', 'observer-long-summary.json'):
                path = private/name
                if path.exists(): reports[name] = json.loads(path.read_text())
            print(json.dumps({'ok': True, 'containers': states, 'reports': reports}, sort_keys=True))
            return 0
        short_spec = inside+'observer-short-spec.json'
        if args.stage in ('short', 'long'):
            long = args.stage == 'long'
            name = 'gdp-consumer-'+tag if long else 'gdp-consumer-'+tag+'-preflight'
            summary = 'observer-long-summary.json' if long else 'observer-short-summary.json'
            trace = 'observer-long-trace.jsonl' if long else 'observer-short-trace.jsonl'
            if (private/summary).exists() or (not long and (private/trace).exists()):
                raise ValueError('OUTPUT_EXISTS')
            if long:
                previous = json.loads((private/'observer-short-summary.json').read_text())
                replay = json.loads((private/'observer-short-replay-summary.json').read_text())
                if not all(result.get('ok') is True for result in (previous, replay)):
                    raise ValueError('PREFLIGHT_NOT_PASSED')
            command = ['python3', 'runtime/rgbd_acceptance.py', '--spec',
                       inside+'observer-long-spec.json' if long else short_spec,
                       '--seconds', '1800' if long else '30', '--discovery-seconds', '8', '--tf-delay-seconds', '2',
                       '--summary', inside+summary]
            if not long:
                command += ['--record', inside+trace, '--max-record-mib', '256']
            # All arguments here are generated constants; no private strings are
            # interpolated into a shell command.
            shell = 'source /opt/ros/humble/setup.bash && exec ' + ' '.join(command)
            call(['docker', 'run', '-d', '--init', '--name', name, '--network', 'host', '--ipc', 'private',
                  '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--entrypoint', '/bin/bash',
                  '--workdir', '/test/source', '-v', str(args.root)+':/test:rw',
                  '-e', 'ROS_DOMAIN_ID='+str(args.domain), '-e', 'ROS_LOCALHOST_ONLY=0',
                  '-e', 'RMW_IMPLEMENTATION=rmw_fastrtps_cpp', '-e', 'FASTRTPS_DEFAULT_PROFILES_FILE=/app/fastdds.xml',
                  args.image, '-lc', shell])
            print(json.dumps({'ok': True, 'container_started': True, 'stage': args.stage, 'duration_seconds': 1800 if long else 30}))
            return 0
        if args.stage == 'replay':
            command = ['python3', 'runtime/rgbd_acceptance.py', '--spec', short_spec,
                       '--replay', inside+'observer-short-trace.jsonl',
                       '--summary', inside+'observer-short-replay-summary.json']
            name = None
        else:
            command = ['python3', 'tools/validation/rosbag_roundtrip.py', '--spec', short_spec,
                       '--bag', inside+'rosbag-short', '--seconds', '5', '--max-mib', '1024']
            name = 'gdp-consumer-'+tag+'-bag'
        docker = ['docker', 'run', '--rm', '--init', '--network', 'host', '--ipc', 'private',
                  '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--entrypoint', '/bin/bash',
                  '--workdir', '/test/source', '-v', str(args.root)+':/test:rw',
                  '-e', 'ROS_DOMAIN_ID='+str(args.domain), '-e', 'ROS_LOCALHOST_ONLY=0',
                  '-e', 'RMW_IMPLEMENTATION=rmw_fastrtps_cpp', '-e', 'FASTRTPS_DEFAULT_PROFILES_FILE=/app/fastdds.xml']
        if name: docker += ['--name', name]
        result = subprocess.run(docker+[args.image, '-lc', 'source /opt/ros/humble/setup.bash && exec '+' '.join(command)],
                                capture_output=True, text=True)
        # Underlying ROS logs can contain private paths. Only emit the tool's
        # final structured allowlisted report, retaining no raw log publicly.
        report = None
        for line in reversed(result.stdout.splitlines()):
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict) and candidate.get('version') == 1:
                    report = candidate; break
            except ValueError: pass
        if report is None: raise RuntimeError('CONSUMER_SUBPROCESS_FAILED')
        if args.stage == 'bag':
            descriptor = os.open(private/'bag-roundtrip-summary.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as handle: json.dump(report, handle, sort_keys=True)
        print(json.dumps(report, sort_keys=True))
        return 0 if result.returncode == 0 and report.get('ok') is True else 1
    except Exception as exc:
        allowed = {'RUNNER_ARGUMENT', 'OUTPUT_EXISTS', 'PREFLIGHT_NOT_PASSED', 'CONTROL_COMMAND_FAILED', 'CONSUMER_SUBPROCESS_FAILED'}
        print(json.dumps({'ok': False, 'stage': args.stage, 'errors': [str(exc) if str(exc) in allowed else 'RUNNER_FAILED']}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
