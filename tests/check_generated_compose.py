"""CI validates a generated three-camera bundle with Compose, without Docker daemon/device access."""
import legacy_support  # Enable the test-only Python behavior oracle.
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from test_configuration_v2 import fixture, confirmed
import cameractl as ctl


def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.object(ctl, 'ROOT', root), patch.object(ctl, 'INV', root / 'inventory.json'), \
             patch.object(ctl, 'assert_stopped'):
            data = confirmed(fixture())
            ctl._atomic_inventory(data)
            record = ctl.render(data)
            if len(record['cameras']) != 3:
                raise RuntimeError('Synthetic bundle must contain three cameras')
            ctl.check_render(data)
    print('Generated synthetic three-camera Compose schema passed; no hardware accessed.')


if __name__ == '__main__': main()
