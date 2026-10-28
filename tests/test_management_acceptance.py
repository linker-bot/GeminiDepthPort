"""Prepare-only harness contracts. No SDK, USB, Docker or HTTP calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('management_acceptance', BASE / 'tools/validation/management_acceptance.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def source_inventory():
    value = json.loads((BASE / 'inventory.v2.example.json').read_text())
    camera = value['cameras'][0]
    camera.update(enabled=True, serial_number='SYNTHETIC_ONLY', profiles_verified=True,
                  verification={'method': 'untrusted-old-proof'}, timestamp_source='global')
    return value


class ManagementAcceptanceTests(unittest.TestCase):
    def test_preview_logs_are_saved_only_for_this_workspace_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            runner=acceptance.Runner(SimpleNamespace(workspace=root))
            runner.logs.mkdir()
            for owned in (False, True):
                source=(root if owned else root/'other')/'work/web-preview/cam_a/config'
                item={'Config':{'Labels':{'orbbec.web.preview':acceptance.PROJECT}},
                      'Mounts':[{'Source':str(source),'Destination':'/config'}]}
                responses=[SimpleNamespace(returncode=0,stdout=json.dumps([item])),
                           SimpleNamespace(stdout='private diagnostic',stderr='')]
                with patch.object(acceptance.subprocess,'run',side_effect=responses) as called:
                    runner.save_preview_logs('cam_a')
                    self.assertEqual(called.call_count,2 if owned else 1)
                path=runner.logs/'preview-cam_a-private.log'
                self.assertEqual(path.exists(),owned)
                if owned:self.assertEqual(path.read_text(),'private diagnostic')

    def test_preparation_never_writes_positive_confirmation(self):
        source = source_inventory()
        result = acceptance.candidate(source, 'local/synthetic:rust', {'explicit': 'policy'})
        self.assertTrue(source['cameras'][0]['profiles_verified'])
        self.assertFalse(result['cameras'][0]['profiles_verified'])
        self.assertIsNone(result['cameras'][0]['verification'])
        self.assertEqual(result['cameras'][0]['timestamp_source'], 'host')
        self.assertEqual(result['cameras'][0]['serial_number'], source['cameras'][0]['serial_number'])

    def test_source_parameters_are_not_silently_dropped(self):
        source = source_inventory()
        source['cameras'][0]['parameters']['color_exposure'] = 100
        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'SOURCE_UNSUPPORTED_PARAMETERS'):
            acceptance.candidate(source, 'local/synthetic:rust', {})

    def test_existing_workspace_is_never_adopted_or_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.json'
            source.write_text(json.dumps(source_inventory()))
            workspace = root / 'existing'
            workspace.mkdir()
            runner = acceptance.Runner(SimpleNamespace(workspace=workspace, source_inventory=source,
                policy_file=None, image='local/synthetic:rust', manager_binary=Path('/unused'), execute=False))
            data = source_inventory(); data['rust_capture'] = {}
            source.write_text(json.dumps(data))
            with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'WORKSPACE_EXISTS'):
                runner.prepare()
            self.assertFalse(runner.owns_workspace)
            self.assertEqual(list(workspace.iterdir()), [])

    def test_prepare_only_cannot_start_manager_or_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.json'
            data = source_inventory(); data['rust_capture'] = {'explicit': 'policy'}
            source.write_text(json.dumps(data))
            binary = root / 'synthetic-manager'; binary.write_bytes(b'synthetic-no-execution')
            runner = acceptance.Runner(SimpleNamespace(workspace=root / 'new', source_inventory=source,
                policy_file=None, image='local/synthetic:rust', manager_binary=binary, execute=False))
            with patch.object(runner, 'cli', return_value={'valid': True}) as cli:
                runner.prepare()
            cli.assert_called_once_with('draft-validate', 'validate', '--draft')
            self.assertFalse(runner.claimed_project)
            self.assertIsNone(runner.process)
            saved = json.loads((runner.workspace / 'inventory.json').read_text())
            self.assertFalse(saved['cameras'][0]['profiles_verified'])


if __name__ == '__main__':
    unittest.main()
