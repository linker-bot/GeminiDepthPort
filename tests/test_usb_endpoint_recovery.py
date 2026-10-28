"""Compile the same recovery algorithm against injected operations; no USB access."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
class RecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
    def test_acquire_complete_scope_before_clear_and_release_every_owned_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/'recovery-test'
            subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror',str(ROOT/'native/usb-recovery/recovery_test.cpp'),'-o',str(binary)],check=True,capture_output=True)
            subprocess.run([str(binary)],check=True,capture_output=True,timeout=5)
