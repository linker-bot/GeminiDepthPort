#!/usr/bin/env python3
"""Explicit administrator USBFS change/restore. Default is read-only plan.

No service reload, host restart or module reload. --root exists for isolated
filesystem tests/chroots; every traversed component must be real, never symlink.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

PARAMETER = 'sys/module/usbcore/parameters/usbfs_memory_mb'
PERSISTENT = 'etc/modprobe.d/geminidepthport-usbfs.conf'
RECEIPT = 'var/lib/geminidepthport/usbfs-receipt.json'


def require(value, code):
    if not value: raise ValueError(code)


def rooted(root, relative, parents=False):
    require(root.is_absolute() and '..' not in root.parts and not root.is_symlink(), 'ROOT_INVALID')
    check=Path(root.anchor)
    for part in root.parts[1:]:
        check /= part
        require(not check.is_symlink(), 'ROOT_SYMLINK')
    root = root.resolve(strict=True)
    current = root
    parts = Path(relative).parts
    require(not Path(relative).is_absolute() and '..' not in parts, 'ROOT_ESCAPE')
    for part in parts[:-1]:
        current /= part
        if not current.exists():
            require(parents, 'ROOT_PARENT_MISSING')
            current.mkdir(mode=0o700)
        require(current.is_dir() and not current.is_symlink(), 'ROOT_SYMLINK')
    path = root / relative
    require(not path.is_symlink(), 'ROOT_SYMLINK')
    return path


def atomic(path, data, mode):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.usbfs-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode); handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def run(args):
    parameter = rooted(args.root, PARAMETER)
    before = int(parameter.read_text().strip())
    require(0 <= before <= 1048576 and 0 <= args.expected_current <= 1048576, 'USBFS_VALUE_INVALID')
    require(before == args.expected_current, 'USBFS_CURRENT_DRIFT')
    require(not (args.apply and args.restore), 'USBFS_ACTION_INVALID')
    if not (args.apply or args.restore):
        return {'version':1, 'ok':True, 'plan_only':True, 'current_mb':before,
                'requested_mb':args.megabytes, 'persistent_requested':args.persist, 'host_written':False}
    require(administrator(), 'ADMINISTRATOR_REQUIRED')
    require(args.management_root is not None, 'MANAGEMENT_ROOT_REQUIRED')
    lock_path=rooted(args.management_root,'state/control.lock')
    lock=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock,'a'):
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('CONTROL_BUSY')
        guard=rooted(args.management_root,'state/recording-guard.json')
        require(not guard.exists() and not guard.is_symlink(), 'RECORDING_PROTECTED')
        require(int(parameter.read_text().strip())==before,'USBFS_CURRENT_DRIFT')
        return mutate(args,parameter,before)


def administrator():return os.geteuid()==0


def mutate(args,parameter,before):
    receipt = rooted(args.root, RECEIPT, parents=True)
    persistent = rooted(args.root, PERSISTENT, parents=True)
    if args.restore:
        require(receipt.is_file() and stat.S_IMODE(receipt.stat().st_mode) == 0o600
                and receipt.stat().st_uid == os.geteuid(), 'USBFS_RECEIPT_PRIVATE_REQUIRED')
        record = json.loads(receipt.read_text())
        require(record['version'] == 1 and record['root'] == str(args.root.resolve())
                and before in (record['previous_mb'],record['applied_mb']), 'USBFS_RECEIPT_MISMATCH')
        if record['persistent']:
            current=persistent.read_text() if persistent.exists() else None
            require(current==record['previous_persistent'] or (current is not None and hashlib.sha256(current.encode()).hexdigest()==record['persistent_sha256']), 'USBFS_PERSISTENT_DRIFT')
        # Restore only after validating all of the scope. Runtime first; a
        # failed persistent restore retains the receipt for explicit recovery.
        with parameter.open('w') as handle: handle.write(str(record['previous_mb'])+'\n')
        require(int(parameter.read_text().strip())==record['previous_mb'],'USBFS_RESTORE_NOT_OBSERVED')
        if record['persistent']:
            if record['previous_persistent'] is None:
                if persistent.exists():persistent.unlink()
            else: atomic(persistent, record['previous_persistent'].encode(), 0o644)
        receipt.unlink()
        return {'version':1, 'ok':True, 'restored':True, 'current_mb':record['previous_mb'], 'host_written':True, 'restart_performed':False}
    require(args.megabytes is not None and 1 <= args.megabytes <= 65536, 'USBFS_REQUEST_INVALID')
    require(not receipt.exists(), 'USBFS_RECEIPT_EXISTS_RESTORE_FIRST')
    old = persistent.read_text() if persistent.exists() else None
    data = ('# Explicit GeminiDepthPort administrator setting; no automatic reload\noptions usbcore usbfs_memory_mb='+str(args.megabytes)+'\n').encode()
    record = {'version':1, 'root':str(args.root.resolve()), 'previous_mb':before,
              'applied_mb':args.megabytes, 'persistent':args.persist,
              'previous_persistent':old, 'persistent_sha256':hashlib.sha256(data).hexdigest()}
    # Recovery evidence precedes the first write, including partial failures.
    atomic(receipt, json.dumps(record, sort_keys=True).encode(), 0o600)
    with parameter.open('w') as handle: handle.write(str(args.megabytes)+'\n')
    require(int(parameter.read_text().strip())==args.megabytes,'USBFS_APPLY_NOT_OBSERVED')
    if args.persist: atomic(persistent, data, 0o644)
    return {'version':1, 'ok':True, 'applied':True, 'current_mb':args.megabytes,
            'persistent_written':args.persist, 'persistence_scope':'modprobe-module-load-options', 'builtin_boot_application_verified':False, 'host_written':True, 'restart_performed':False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('/'))
    p.add_argument('--management-root',type=Path)
    p.add_argument('--expected-current',type=int,required=True)
    p.add_argument('--megabytes',type=int)
    p.add_argument('--persist',action='store_true')
    p.add_argument('--apply',action='store_true')
    p.add_argument('--restore',action='store_true')
    args=p.parse_args(argv)
    try: report=run(args)
    except Exception as error:
        code=str(error) if type(error) is ValueError and str(error).isupper() else 'USBFS_CONTROL_FAILED'
        report={'version':1,'ok':False,'errors':[code],'restart_performed':False}
    print(json.dumps(report,sort_keys=True));return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
