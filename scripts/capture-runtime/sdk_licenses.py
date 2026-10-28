"""Collect and verify public SDK license files without loading native code."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

SDK_SOURCE = {
    'repository': 'https://github.com/orbbec/OrbbecSDK_v2',
    'version': '2.10.6',
    'commit': '575347ca742d219ef61b1e214e97043f17f48d23',
    'path': 'LICENSE.txt',
    'sha256': '010b2381c54551d1928784124282a7ff8cf6786f01846179bde3f810b7cd6b30',
}
ROS_COMMIT = 'c153462518ad674650bafa4464fda72c27ab797a'
REQUIRED = {
    'SDK-LICENSE.txt', 'SDK-LICENSE-SOURCE.json', 'ROS2-LICENSE', 'ROS2-NOTICE',
    'sdk-licenses/THIRD_PARTY_LICENSES.md', 'sdk-licenses/libusb/COPYING',
    'sdk-licenses/live555/COPYING', 'sdk-licenses/live555/COPYING.LESSER',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(path):
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError('LICENSE_FILE_MISSING_OR_INVALID')


def verify(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('LICENSE_DIRECTORY_INVALID')
    regular(root / 'license-inventory.json')
    record = json.loads((root / 'license-inventory.json').read_text())
    files = record.get('files')
    if (record.get('version') != 1 or record.get('upstream_commit') != ROS_COMMIT
            or record.get('sdk_license_source') != SDK_SOURCE or not isinstance(files, dict)
            or not REQUIRED.issubset(files)):
        raise ValueError('LICENSE_INVENTORY_INCOMPLETE')
    for relative, expected in files.items():
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or '..' in parts:
            raise ValueError('LICENSE_INVENTORY_PATH')
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError('LICENSE_INVENTORY_LINK')
        regular(path)
        if digest(path) != expected:
            raise ValueError('LICENSE_HASH_MISMATCH')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual != set(files) | {'license-inventory.json'}:
        raise ValueError('LICENSE_INVENTORY_FILE_SET')
    if (digest(root / 'SDK-LICENSE.txt') != SDK_SOURCE['sha256']
            or json.loads((root / 'SDK-LICENSE-SOURCE.json').read_text()) != SDK_SOURCE):
        raise ValueError('SDK_LICENSE_SOURCE_MISMATCH')
    return record


def collect(source, license_root, output):
    source, license_root, output = map(Path, (source, license_root, output))
    third_party = source / 'orbbec_camera/SDK/licenses'
    if not third_party.is_dir() or third_party.is_symlink():
        raise ValueError('SDK_LICENSE_DIRECTORY_MISSING')
    selected = {
        'ROS2-LICENSE': source / 'LICENSE',
        'ROS2-NOTICE': source / 'NOTICE',
        'SDK-LICENSE.txt': license_root / 'LICENSE.txt',
        'SDK-LICENSE-SOURCE.json': license_root / 'source.json',
    }
    for path in sorted(third_party.rglob('*')):
        if path.is_symlink():
            raise ValueError('SDK_LICENSE_SYMLINK')
        if path.is_file():
            selected['sdk-licenses/' + path.relative_to(third_party).as_posix()] = path
    if not REQUIRED.issubset(selected):
        raise ValueError('SDK_LICENSE_SET_INCOMPLETE')
    for path in selected.values():
        regular(path)
    if (digest(license_root / 'LICENSE.txt') != SDK_SOURCE['sha256']
            or json.loads((license_root / 'source.json').read_text()) != SDK_SOURCE):
        raise ValueError('SDK_LICENSE_SOURCE_MISMATCH')
    # Fail before writing any output on missing licenses or invalid provenance.
    output.mkdir(parents=True, exist_ok=False)
    try:
        for relative, original in selected.items():
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        record = {'version': 1, 'upstream_commit': ROS_COMMIT,
                  'sdk_license_source': SDK_SOURCE,
                  'files': {name: digest(output / name) for name in sorted(selected)}}
        (output / 'license-inventory.json').write_text(json.dumps(record, sort_keys=True, indent=2) + '\n')
        verify(output)
    except BaseException:
        shutil.rmtree(output)
        raise
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root')
    parser.add_argument('--license-root')
    parser.add_argument('--output', required=True)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        record = verify(args.output)
    else:
        if not args.source_root or not args.license_root:
            parser.error('--source-root and --license-root are required for collection')
        record = collect(args.source_root, args.license_root, args.output)
    print(json.dumps({'ok': True, 'license_files': len(record['files'])}))


if __name__ == '__main__':
    main()
