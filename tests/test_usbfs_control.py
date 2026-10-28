"""Isolated filesystem/lock tests only; never touches real sysfs or modprobe."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import fcntl
import json
import os
BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('usbfs_control',BASE/'scripts/host/usbfs_control.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
class UsbfsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.parameter=self.root/control.PARAMETER
        self.parameter.parent.mkdir(parents=True);self.parameter.write_text('16\n')
        self.management=self.root/'management';(self.management/'state').mkdir(parents=True)
    def args(self,**kw):
        result=dict(root=self.root,management_root=self.management,expected_current=16,megabytes=256,persist=True,apply=False,restore=False)
        result.update(kw);return SimpleNamespace(**result)
    def test_default_is_pure_plan_even_if_persistence_requested(self):
        before=sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        report=control.run(self.args());self.assertFalse(report['host_written'])
        self.assertEqual(before,sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))
        self.assertEqual(self.parameter.read_text(),'16\n')
    def test_admin_apply_and_restore_original_runtime_and_config(self):
        persistent=self.root/control.PERSISTENT;persistent.parent.mkdir(parents=True);persistent.write_text('# existing administrator config\n')
        with patch.object(control,'administrator',return_value=True):
            control.run(self.args(apply=True))
            self.assertEqual(self.parameter.read_text(),'256\n')
            receipt=self.root/control.RECEIPT;self.assertEqual(receipt.stat().st_mode&0o777,0o600)
            control.run(self.args(restore=True,expected_current=256))
        self.assertEqual(self.parameter.read_text(),'16\n');self.assertEqual(persistent.read_text(),'# existing administrator config\n');self.assertFalse(receipt.exists())
    def test_recording_guard_and_lifecycle_lock_block_host_mutation(self):
        with patch.object(control,'administrator',return_value=True):
            guard=self.management/'state/recording-guard.json';guard.write_text('malformed')
            with self.assertRaisesRegex(ValueError,'RECORDING_PROTECTED'):control.run(self.args(apply=True))
            guard.unlink()
            with (self.management/'state/control.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError,'CONTROL_BUSY'):control.run(self.args(apply=True))
        self.assertEqual(self.parameter.read_text(),'16\n')
    def test_symlink_parameter_root_and_persistent_parent_are_rejected(self):
        target=self.root/'outside';target.write_text('unchanged')
        self.parameter.unlink();self.parameter.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'ROOT_SYMLINK'):control.run(self.args())
        self.parameter.unlink();self.parameter.write_text('16')
        etc=self.root/'etc';etc.symlink_to(self.root/'management')
        with patch.object(control,'administrator',return_value=True):
            with self.assertRaisesRegex(ValueError,'ROOT_SYMLINK'):control.run(self.args(apply=True))
        self.assertEqual(target.read_text(),'unchanged')
    def test_cas_and_unmanaged_persistent_drift_reject_restore(self):
        with self.assertRaisesRegex(ValueError,'USBFS_CURRENT_DRIFT'):control.run(self.args(expected_current=17))
        with patch.object(control,'administrator',return_value=True):
            control.run(self.args(apply=True));(self.root/control.PERSISTENT).write_text('someone else changed this')
            with self.assertRaisesRegex(ValueError,'USBFS_PERSISTENT_DRIFT'):control.run(self.args(restore=True,expected_current=256))
        self.assertEqual(self.parameter.read_text(),'256\n');self.assertTrue((self.root/control.RECEIPT).exists())
    def test_partial_prepare_receipt_can_be_restored_without_overwriting_third_party(self):
        receipt=self.root/control.RECEIPT;receipt.parent.mkdir(parents=True)
        record={'version':1,'root':str(self.root),'previous_mb':16,'applied_mb':256,'persistent':True,'previous_persistent':None,'persistent_sha256':'0'*64}
        receipt.write_text(json.dumps(record));receipt.chmod(0o600)
        with patch.object(control,'administrator',return_value=True):control.run(self.args(restore=True))
        self.assertFalse(receipt.exists());self.assertEqual(self.parameter.read_text(),'16\n')
if __name__=='__main__':unittest.main()
