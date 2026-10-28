import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'tools/validation/sdk_reopen_control.py'
spec = importlib.util.spec_from_file_location('sdk_reopen_control', MODULE)
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class ReopenControlTests(unittest.TestCase):
    def fixture(self, parent):
        parent = Path(parent)
        config = parent / 'config.json'
        config.write_text('{"serial":"private-test-id"}')
        binary = parent / 'fake-sdk'
        binary.write_text('''#!/usr/bin/env python3
import pathlib, sys, time
out=pathlib.Path(sys.argv[2]);out.mkdir()
if len(sys.argv)==4:
 (out/'READY').write_text('ready')
 while True:time.sleep(.1)
if out.name=='reopen-1':sys.exit(1)
(out/'READY').write_text('ready')
(out/'CAPTURE_COMPLETE').write_text('complete')
''')
        binary.chmod(0o700)
        return SimpleNamespace(config=config, binary=binary, output_dir=parent/'result',
                               retries=2, execute=False, devices_released=False)

    def test_plan_never_spawns_or_creates_output(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.fixture(root)
            result = control.run(args)
            self.assertFalse(result['hardware_executed'])
            self.assertFalse(args.output_dir.exists())

    def test_requires_explicit_released_ack_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.fixture(root)
            args.execute = True
            with self.assertRaisesRegex(ValueError, 'DEVICES_RELEASED'):
                control.run(args)
            self.assertFalse(args.output_dir.exists())

    def test_real_child_kill_then_distinct_failed_and_successful_reopens(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.fixture(root)
            args.execute = args.devices_released = True
            result = control.run(args)
            self.assertTrue(result['initial_ready'])
            self.assertTrue(result['initial_sigkill'])
            self.assertEqual([x['returncode'] for x in result['attempts']], [1, 0])
            self.assertEqual([x['frameset_ready'] for x in result['attempts']], [False, True])
            self.assertEqual([x['normal_exit'] for x in result['attempts']], [False, True])
            self.assertNotIn('private-test-id', str(result))
            self.assertEqual((args.output_dir/'capture.json').read_bytes(), args.config.read_bytes())
            self.assertEqual(args.output_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((args.output_dir/'capture.json').stat().st_mode & 0o777, 0o600)
            self.assertTrue((args.output_dir/'report.json').is_file())


if __name__ == '__main__':
    unittest.main()
