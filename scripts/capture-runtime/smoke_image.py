"""No-device capture-image contract and executable compatibility smoke test."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def main():
    manifest = json.loads(Path('/opt/gdp-capture/runtime.json').read_text())
    require(manifest['version'] == 1 and manifest['sdk_version'] == '2.10.6'
            and manifest['upstream_commit'] == 'c153462518ad674650bafa4464fda72c27ab797a', 'runtime source/version differs')
    source = manifest['sdk_source']
    require(source['repository'] == 'https://github.com/orbbec/OrbbecSDK_ROS2'
            and source['commit'] == manifest['upstream_commit'] and source['license_files'], 'source/license inventory missing')
    for name, expected in manifest['files'].items():
        require(hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, 'runtime file hash differs')
    for name, expected in source['license_files'].items():
        require(hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, 'license hash differs')
    sdk = '/opt/orbbec-sdk/lib/libOrbbecSDK.so'
    bridge = '/opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so'
    service = '/usr/local/bin/capture-service'
    for path, key in ((sdk, 'sdk_sha256'), (bridge, 'bridge_sha256'), (service, 'service_sha256')):
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest() == manifest[key] == manifest['files'][path], 'binary identity differs')
    require(hashlib.sha256(Path('/opt/orbbec-sdk/lib/libOrbbecSDK.so.2.10.6').read_bytes()).hexdigest()
            == manifest['sdk_sha256'], 'actual SDK version file differs')
    with tempfile.TemporaryDirectory(prefix='capture-image-smoke-', dir='/tmp') as directory:
        config = {'version': 1, 'sdk_library': sdk, 'serial': 'SYNTHETIC_NO_DEVICE_ACCESS',
                  'color': {'width': 2, 'height': 2, 'fps': 30, 'frame_id': 'synthetic_color_optical'},
                  'depth': {'width': 2, 'height': 2, 'fps': 30, 'frame_id': 'synthetic_depth_optical', 'unit_policy': 'normalize-mm'},
                  'pairing': {'clock': 'device', 'max_delta_us': 20000}, 'poll_ms': 100, 'capacity': 4,
                  'startup_discovery_seconds': 2,
                  'ros': {'bridge': bridge, 'namespace': '/synthetic_no_device', 'stamp_source': 'sdk-system-unix'},
                  'runtime_contract': {key: manifest[key] for key in ('sdk_sha256', 'bridge_sha256', 'service_sha256')},
                  'service': {'status_path': directory + '/status/status.json', 'generation': 'synthetic-image-smoke',
                              'idle_timeout_ms': 2000, 'startup_timeout_ms': 15000, 'heartbeat_ms': 500,
                              'heartbeat_timeout_ms': 10000, 'max_restarts': 3, 'restart_backoff_ms': 1000,
                              'shutdown_timeout_ms': 5000, 'ready_min_pairs': 5}}
        path = Path(directory) / 'smoke.json'
        path.write_text(json.dumps(config))
        result = subprocess.run([service, '--validate-config', str(path)], text=True, capture_output=True, timeout=15)
        require(result.returncode == 0, 'capture executable/contract check failed: ' + result.stderr[:1000])
        report = json.loads(result.stdout)
        require(report.get('valid') is True and report.get('runtime_contract_verified') is True
                and report.get('hardware_verified') is False, 'validation report differs')
        config['runtime_contract']['sdk_sha256'] = '0' * 64
        path.write_text(json.dumps(config))
        rejected = subprocess.run([service, '--validate-config', str(path)], capture_output=True, timeout=15)
        require(rejected.returncode != 0, 'tampered binary contract accepted')
    print(json.dumps({'ok': True, 'kind': 'capture-image-no-device-smoke', 'runtime_contract_verified': True,
                      'binary_compatible': True, 'tamper_rejected': True, 'hardware_verified': False}))


if __name__ == '__main__':
    main()
