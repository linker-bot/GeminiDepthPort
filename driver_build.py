"""Public source/patch identity used by image generation and preflight."""
import hashlib
import json
from pathlib import Path


def patch_identity():
    folder = Path(__file__).parent / 'patches/public-orbbec'
    manifest = json.loads((folder / 'manifest.json').read_text())
    patch = folder / manifest['patch_file']
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest['patch_sha256']:
        raise ValueError('Public patch identity mismatch')
    return manifest
