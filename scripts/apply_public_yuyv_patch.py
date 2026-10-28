#!/usr/bin/env python3
"""Apply an exact, publicly sourced patch; refuse unknown or already modified input."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

DEFAULT_BUNDLE = Path(__file__).resolve().parents[1] / 'patches/public-orbbec'


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_files(root, files):
    for relative, expected in files.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or digest(root / path) != expected:
            raise ValueError('Public driver source digest mismatch')


def process(root, action, upstream_commit, bundle=DEFAULT_BUNDLE):
    root = Path(root); bundle = Path(bundle)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if upstream_commit != manifest['upstream_commit']:
        raise ValueError('Unsupported public driver baseline')
    patch = bundle / manifest['patch_file']
    if digest(patch) != manifest['patch_sha256']:
        raise ValueError('Public patch digest mismatch')
    environment = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.resolve().parent))
    environment.pop('GIT_DIR', None); environment.pop('GIT_WORK_TREE', None)
    if action == 'verify':
        verify_files(root, manifest['patched_files'])
    else:
        verify_files(root, manifest['original_files'])
        subprocess.run(['git', 'apply', '--no-index', '--check', str(patch.resolve())], cwd=root, check=True, env=environment,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if action == 'apply':
            subprocess.run(['git', 'apply', '--no-index', str(patch.resolve())], cwd=root, check=True, env=environment,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            verify_files(root, manifest['patched_files'])
        elif action != 'check':
            raise ValueError('Unsupported patch action')
    return {'upstream_commit': manifest['upstream_commit'], 'patch_sha256': manifest['patch_sha256'],
            'source_verified': True, 'hardware_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--upstream-commit', required=True)
    parser.add_argument('--action', choices=['check', 'apply', 'verify'], required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(process(args.source_root, args.action, args.upstream_commit), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(1, 'Public YUYV source/patch verification failed.\n')


if __name__ == '__main__': main()
