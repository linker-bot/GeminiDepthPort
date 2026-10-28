#!/usr/bin/env python3
"""One locked checked startup path for CLI, Web and boot; no unconditional restart."""
import secrets
import sys
import time
import cameractl as ctl
from web_server import Manager


def start(mode):
    with ctl.control_lock():
        ctl.guard.assert_unprotected(ctl.ROOT)
        data = ctl.load()
        try:
            ctl.validate(data)
        except ctl.ConfigurationError:
            if mode == 'boot':
                print('Management is available; configuration verification required.', flush=True)
                return 0
            raise
        manager = Manager()
        job = {'id': secrets.token_hex(6), 'action': mode, 'status': 'running', 'started_at': time.time(),
               'inventory_revision': ctl.digest(ctl.INV), 'log': ['Checked startup requested.']}
        if mode == 'boot': manager.persist_job(job)
        try:
            # Previews are never terminated implicitly. Stop them explicitly before formal startup.
            ctl.ensure_started(data)
            job['status'] = 'passed'; job['log'].append('All required streams passed actual checks.')
            return 0
        except Exception:
            job['status'] = 'failed'; job['log'].append('Startup failed; inspect controlled lifecycle status.')
            raise
        finally:
            job['finished_at'] = time.time()
            if mode == 'boot': manager.persist_job(job)


if __name__ == '__main__':
    try:
        mode = sys.argv[1] if len(sys.argv) == 2 else ''
        if mode not in ('up', 'boot'): raise ValueError('Expected up or boot')
        raise SystemExit(start(mode))
    except (ValueError, OSError, RuntimeError):
        print('ERROR: Checked startup failed; inspect controlled status.', file=sys.stderr)
        raise SystemExit(1)
