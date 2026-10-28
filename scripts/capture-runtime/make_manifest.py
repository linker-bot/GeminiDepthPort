"""Build-time public runtime inventory; no device or private input access."""
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


files = {}
for folder in ('/opt/orbbec-sdk', '/opt/gdp-ros', '/opt/third-party/orbbec', '/app'):
    for path in sorted(Path(folder).rglob('*')):
        if path.is_file():
            files[str(path)] = digest(path)
files['/usr/local/bin/capture-service'] = digest('/usr/local/bin/capture-service')
manifest = {
    'version': 1, 'upstream_commit': Path('/opt/orbbec-commit').read_text().strip(),
    'sdk_version': '2.10.6', 'communication_profile': 'humble-fastdds-local-udp-v1',
    'sdk_sha256': digest('/opt/orbbec-sdk/lib/libOrbbecSDK.so'),
    'bridge_sha256': digest('/opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so'),
    'service_sha256': files['/usr/local/bin/capture-service'], 'files': files,
    'sdk_source': {'repository': 'https://github.com/orbbec/OrbbecSDK_ROS2',
                   'commit': Path('/opt/orbbec-commit').read_text().strip(),
                   'license_files': {k: v for k, v in files.items() if k.startswith('/opt/third-party/orbbec/')
                                     or 'license' in Path(k).name.lower() or 'notice' in Path(k).name.lower()}},
}
Path('/opt/gdp-capture/runtime.json').write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n')
