"""Build-time public runtime inventory; no device or private input access."""
import hashlib
import json
from pathlib import Path

from sdk_licenses import verify


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# Require a complete pinned license set before declaring a runtime license inventory.
license_inventory = verify('/opt/third-party/orbbec')
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
                   'license_source': license_inventory['sdk_license_source'],
                   'license_files': {k: v for k, v in files.items() if k.startswith('/opt/third-party/orbbec/')
                                     or 'license' in Path(k).name.lower() or 'notice' in Path(k).name.lower()}},
}
helper = '/opt/gdp-ros/bin/gdp-usb-recover'
if Path(helper).is_file():
    manifest['usb_recovery'] = {'helper': helper, 'sha256': files[helper]}
Path('/opt/gdp-capture/runtime.json').write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n')
