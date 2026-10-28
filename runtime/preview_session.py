"""Own both driver and subscriber; terminate process groups together on stop/failure."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    children = []
    def stop(signum=None, frame=None):
        for child in children:
            if child.poll() is None:
                try: os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError: pass
        deadline = time.monotonic() + 8
        for child in children:
            try: child.wait(timeout=max(0.1, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                try: os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
        if signum is not None: raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        children.append(subprocess.Popen(['/app/entrypoint.sh'], start_new_session=True))
        children.append(subprocess.Popen([sys.executable, '/app/preview_bridge.py'], start_new_session=True))
        while all(child.poll() is None for child in children):
            # Host browser requests renew this file; also expires if the web server crashes.
            lease = Path('/config/lease')
            if not lease.exists() or time.time() - lease.stat().st_mtime > 125:
                print('Preview lease expired; releasing camera.', flush=True)
                return 0
            time.sleep(0.2)
        return 1
    finally: stop()

if __name__ == '__main__': raise SystemExit(main())
