"""Rust service status AND actual ROS consumption; neither alone is readiness."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def process_alive(pid):
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def worker_parent_alive(pid):
    try:
        stat = Path('/proc/' + str(pid) + '/stat').read_text()
        parent = int(stat[stat.rfind(')') + 2:].split()[1])
        if parent < 1:
            return False
        os.kill(parent, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def validate_snapshot(config_bytes, status, now_ms, alive=process_alive, parent_alive=worker_parent_alive):
    config = json.loads(config_bytes)
    service = config['service']
    valid_integer = lambda value: type(value) is int and value >= 0
    if (type(status.get('version')) is not int or status['version'] != 1
            or status.get('kind') != 'capture-service-status' or status.get('state') != 'ready'
            or status.get('ready') is not True
            or status.get('generation') != service['generation']
            or status.get('config_sha256') != hashlib.sha256(config_bytes).hexdigest()
            or not isinstance(status.get('session_id'), str)
            or not re.fullmatch('[a-f0-9]{32}', status['session_id'])
            or not valid_integer(status.get('heartbeat_sequence')) or status['heartbeat_sequence'] < 1
            or not valid_integer(status.get('updated_unix_ms'))
            or not valid_integer(status.get('last_pair_age_ms'))
            or not alive(status.get('pid')) or not parent_alive(status['pid'])):
        raise ValueError('RUST_STATUS_INVALID')
    age = now_ms - status['updated_unix_ms']
    if (age < -100 or age > service['heartbeat_timeout_ms']
            or status['last_pair_age_ms'] + max(age, 0) > service['idle_timeout_ms']):
        raise ValueError('RUST_STATUS_STALE')
    if status.get('supervisor_pid') is not None and not alive(status['supervisor_pid']):
        raise ValueError('RUST_SUPERVISOR_MISSING')
    return status


def validate_transition(first, second):
    if (second['session_id'] != first['session_id'] or second['pid'] != first['pid']
            or second['heartbeat_sequence'] <= first['heartbeat_sequence']):
        raise ValueError('RUST_SESSION_CHANGED_OR_FROZEN')


def main():
    try:
        config_bytes = Path('/config/capture.json').read_bytes()
        config = json.loads(config_bytes)
        path = Path(config['service']['status_path'])
        first = validate_snapshot(config_bytes, json.loads(path.read_text()), time.time_ns() // 1_000_000)
        environment = dict(os.environ, GDP_VALIDATE_HOST_STAMPS='1')
        result = subprocess.run([sys.executable, '/app/ros_health.py'], env=environment, timeout=24)
        if result.returncode:
            raise ValueError('RUST_ROS_CONSUMPTION_FAILED')
        if Path('/config/capture.json').read_bytes() != config_bytes:
            raise ValueError('RUST_CONFIG_CHANGED')
        second = validate_snapshot(config_bytes, json.loads(path.read_text()), time.time_ns() // 1_000_000)
        validate_transition(first, second)
        return 0
    except Exception:
        print('RUST_CAPTURE_UNHEALTHY: Service/session/source-time/ROS verification failed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
