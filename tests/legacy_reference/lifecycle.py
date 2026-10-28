"""Sequential startup and discovery-based shutdown without inventory-driven omissions."""
import json
import re
import time
import generation
from configuration import ConfigurationError, require
from operation_guard import atomic_json


class LifecycleError(ConfigurationError):
    def __init__(self, code, result):
        self.result = result
        super().__init__(code, 'Camera group is not ready; inspect controlled lifecycle status')


def instances(ctl, kind='camera'):
    filters = ['label=com.docker.compose.project=' + ctl.PROJECT, 'label=orbbec.role=' + kind,
               'label=com.docker.compose.oneoff=False']
    if kind == 'preview': filters = ['label=orbbec.web.preview=' + ctl.PROJECT]
    args = ['docker', 'ps', '-aq', '--no-trunc']
    for item in filters: args.extend(['--filter', item])
    ids = ctl.run(args, True).split()
    require(all(re.fullmatch('[a-f0-9]{12,64}', value) for value in ids),
            'INSTANCE_DISCOVERY', 'Invalid managed container reference')
    if not ids: return []
    values = json.loads(ctl.run(['docker', 'inspect', *ids], True))
    require(isinstance(values, list) and len(values) == len(ids), 'INSTANCE_DISCOVERY', 'Incomplete discovery result')
    for item in values:
        labels = item['Config'].get('Labels') or {}
        owned = (labels.get('orbbec.web.preview') == ctl.PROJECT if kind == 'preview' else
                 labels.get('com.docker.compose.project') == ctl.PROJECT
                 and labels.get('orbbec.role') == kind
                 and labels.get('com.docker.compose.oneoff', '').lower() == 'false')
        require(owned and item['Id'] in ids, 'INSTANCE_OWNERSHIP', 'Unmanaged container refused')
    return values


def record(ctl, result):
    atomic_json(ctl.ROOT / 'state/lifecycle.json', result)
    return result


def stop(ctl, kind='camera', remove=False):
    values = instances(ctl, kind)
    failures = []
    for item in values:
        try:
            if item['State'].get('Running'):
                ctl.run(['docker', 'stop', '--time', '30', item['Id']], True)
            if remove:
                ctl.run(['docker', 'rm', item['Id']], True)
        except Exception:
            failures.append('STOP_FAILED')
    remaining = instances(ctl, kind)
    if any(item['State'].get('Running') for item in remaining): failures.append('STOP_INCOMPLETE')
    result = {'ready': False, 'status': 'failed' if failures else 'stopped', 'errors': failures}
    if kind == 'camera': record(ctl, result)
    if failures: raise LifecycleError('STOP_FAILED', result)
    return result


def wait_healthy(ctl, service, timeout):
    cid = ctl.compose('ps', '-a', '-q', service, capture=True).strip()
    require(bool(cid) and re.fullmatch('[a-f0-9]{12,64}', cid), 'CAMERA_MISSING', 'Managed camera instance missing')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = json.loads(ctl.run(['docker', 'inspect', cid], True))[0]['State']
        if state.get('Health', {}).get('Status') == 'healthy' and state.get('Running'): return
        if state.get('Status') in ('exited', 'dead'): break
        time.sleep(0.2)
    raise ConfigurationError('CAMERA_HEALTH_FAILED', 'Actual frame health check did not pass')


def check_existing(ctl, snapshot):
    live = [item for item in instances(ctl) if item['State'].get('Running')]
    expected = {c['name'] for c in snapshot['cameras']}
    names = []
    for item in live:
        labels = item['Config'].get('Labels') or {}
        require(labels.get('orbbec.generation') == snapshot['generation']
                and labels.get('orbbec.camera') in expected,
                'RUNNING_CONFIGURATION_CHANGED', 'Stop the previous camera generation before switching')
        names.append(labels['orbbec.camera'])
    require(len(names) == len(set(names)), 'DUPLICATE_INSTANCE', 'Duplicate running device driver refused')
    return {item['Config']['Labels']['orbbec.camera']: item for item in live}


def probe(ctl, service):
    ctl.compose('exec', '-T', service, '/app/entrypoint.sh', 'python3', '/app/health.py', capture=True)


def start(ctl, data):
    snapshot = ctl.check_render(data)
    states = [{'index': index, 'status': 'not_started'} for index, _ in enumerate(snapshot['cameras'])]
    result = {'ready': False, 'status': 'starting', 'generation': snapshot['generation'], 'cameras': states}
    record(ctl, result)
    active_index = None
    try:
        ctl.preflight(data)
        existing = check_existing(ctl, snapshot)
        for active_index, camera in enumerate(snapshot['cameras']):
            if camera['name'] not in existing:
                ctl.compose('run', '--rm', '--no-deps', camera['service'],
                            'python3', '/app/validate_runtime.py', capture=True)
        for active_index, (state, camera) in enumerate(zip(states, snapshot['cameras'])):
            if camera['name'] in existing:
                require(existing[camera['name']]['State'].get('Health', {}).get('Status') == 'healthy',
                        'CAMERA_HEALTH_FAILED', 'Running camera is unhealthy; explicit recovery required')
            else:
                ctl.compose('up', '-d', '--no-deps', '--no-build', camera['service'], capture=True)
            ctl.healthy(camera['service'], data['startup_timeout_seconds'])
            # A stale Docker healthy flag is insufficient for sequential readiness.
            probe(ctl, camera['service'])
            state['status'] = 'healthy'; record(ctl, result)
        for active_index, camera in enumerate(snapshot['cameras']): probe(ctl, camera['service'])
        current = check_existing(ctl, snapshot)
        require(len(current) == len(snapshot['cameras']), 'GROUP_INCOMPLETE', 'Required camera group incomplete')
        result.update(ready=True, status='ready', reused=len(existing) == len(snapshot['cameras']))
        return record(ctl, result)
    except Exception as exc:
        code = exc.code if isinstance(exc, ConfigurationError) else 'START_FAILED'
        if active_index is not None:
            states[active_index].update(status='failed', error=code)
        result.update(ready=False, status='failed', error=code)
        record(ctl, result)
        raise LifecycleError(code, result) from None


def ensure_started(ctl, data):
    """Never stop first: reuse current healthy instances, require explicit config switch."""
    try:
        try:
            current = ctl.check_render(data)
        except (ConfigurationError, OSError, ValueError):
            # Generation switching is only allowed with no running devices or previews.
            ctl.assert_stopped()
            current = ctl.render(data)
        require(not current['draft'], 'GENERATION_DRAFT', 'Draft generation cannot start cameras')
        return ctl.start_cameras(data)
    except Exception as exc:
        if not isinstance(exc, LifecycleError):
            record(ctl, {'ready': False, 'status': 'failed',
                         'error': exc.code if isinstance(exc, ConfigurationError) else 'START_FAILED'})
        raise
