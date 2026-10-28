"""Validate immutable runtime/configuration only; never opens an SDK device."""
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    try:
        expected = json.loads(Path('/config/runtime.json').read_text())
        actual = json.loads(Path('/opt/gdp-capture/runtime.json').read_text())
        if expected != actual or actual.get('version') != 1 or not actual.get('files'):
            raise ValueError('runtime differs')
        for name, expected_hash in actual['files'].items():
            if not name.startswith(('/opt/orbbec-sdk/', '/opt/gdp-ros/', '/app/', '/opt/third-party/orbbec/', '/usr/local/bin/capture-service')):
                raise ValueError('unexpected runtime path')
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != expected_hash:
                raise ValueError('runtime hash differs')
        subprocess.run(['/usr/local/bin/capture-service', '--validate-config', '/config/capture.json'], check=True)
        print('Rust capture runtime and configuration validated; no device opened.')
        return 0
    except Exception:
        print('RUST_RUNTIME_INVALID: Runtime/configuration verification failed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
