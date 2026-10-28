"""Cross-process lifecycle serialization and persistent recording interlock."""
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from configuration import ConfigurationError, require

_local = threading.local()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, sort_keys=True, allow_nan=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def control_lock(root):
    root = Path(root).resolve()
    held = getattr(_local, 'held', {})
    if root in held:
        yield
        return
    folder = root / 'state'; folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / 'control.lock').open('a') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError('CONTROL_BUSY', 'Another lifecycle operation is in progress') from None
        _local.held = held.copy()
        _local.held[root] = True
        try: yield
        finally:
            _local.held = held
            fcntl.flock(handle, fcntl.LOCK_UN)


def protected(root):
    # Even an unreadable/corrupt record is a protection, never a timeout to ignore.
    path = Path(root) / 'state' / 'recording-guard.json'
    return path.exists() or path.is_symlink()


def assert_unprotected(root):
    require(not protected(root), 'RECORDING_PROTECTED', 'Recording protection blocks this operation')


def acquire(root, generation):
    assert_unprotected(root)
    token = secrets.token_urlsafe(32)
    atomic_json(Path(root) / 'state/recording-guard.json',
                {'version': 1, 'generation': generation, 'token_sha256': hashlib.sha256(token.encode()).hexdigest()})
    return token


def release(root, token):
    path = Path(root) / 'state/recording-guard.json'
    try:
        record = json.loads(path.read_text())
        expected = record['token_sha256']
        valid = isinstance(expected, str) and isinstance(token, str) and hmac.compare_digest(
            expected, hashlib.sha256(token.encode()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError): valid = False
    require(valid, 'GUARD_CREDENTIAL', 'Valid recording protection credential required')
    path.unlink()
