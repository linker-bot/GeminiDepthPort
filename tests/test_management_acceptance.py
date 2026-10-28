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


def native_context(protected=False):
    sid, instance, snapshot = 'a'*32, 'b'*64, 'c'*64
    calibration = dict(source='sdk-active-profiles', actual_runtime_observed=True,
                       physical_accuracy_verified=False, source_session_id=sid, source_instance_id=instance)
    return dict(version=1, kind='recording-context', backend='rust', generation_id='generation',
                physical_accuracy_verified=False, protection=dict(guard_version=2,
                strong_guarantee_available=True, native_restart_gate=True, session_bound=protected,
                native_gate_ack_verified=protected, state='active' if protected else 'unprotected'),
                cameras=[dict(camera_name='cam_a', container_id='d'*64, instance_id=instance,
                session_id=sid, session_kind='sdk-worker', snapshot=dict(version=1,snapshot_id=snapshot,
                content=dict(binding=dict(instance_id=instance,session_id=sid,generation_id='generation'),
                calibration=calibration, configuration=dict(runtime_config_sha256='e'*64))))])


class NewFeatureAcceptanceTests(unittest.TestCase):
    def test_context_requires_real_active_ack_and_same_immutable_runtime(self):
        original = native_context()
        binding = acceptance.context_bindings(original, ['cam_a'], False)
        protected = native_context(True)
        self.assertEqual(acceptance.context_bindings(protected, ['cam_a'], True), binding)
        for field in ('session_bound', 'native_gate_ack_verified', 'native_restart_gate'):
            invalid = json.loads(json.dumps(protected))
            invalid['protection'][field] = False
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.context_bindings(invalid, ['cam_a'], True)
        protected['cameras'][0]['snapshot']['content']['calibration']['source'] = 'ros-camera-info-tf'
        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'SNAPSHOT_BINDING'):
            acceptance.context_bindings(protected, ['cam_a'], True)
        runner = acceptance.Runner(SimpleNamespace(workspace=Path('/unused')))
        changed = native_context(True)
        changed['cameras'][0]['container_id'] = 'f'*64
        with patch.object(runner, 'cli', return_value=changed):
            with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'RUNTIME_CHANGED'):
                runner.verify_context(['cam_a'], True, binding)

    def test_released_context_cannot_keep_stale_positive_ack(self):
        value = native_context(False)
        value['protection']['native_gate_ack_verified'] = True
        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'NATIVE_PROTECTION'):
            acceptance.context_bindings(value, ['cam_a'], False)

    def test_quality_queries_actual_three_endpoints_and_encoded_cursor(self):
        context = native_context()
        bindings = acceptance.context_bindings(context, ['cam_a'], False)
        def response(kind, result):
            return dict(version=1,kind='quality-'+kind,backend='rust',camera_name='cam_a',
                        session_kind='sdk-worker',worker_ready=True,instance_id='b'*64,session_id='a'*32,
                        snapshot_id='c'*64,generation_id='generation',result=result)
        cursor = dict(origin_session_id='a'*32, after_sequence=1)
        responses = [response('current',dict(quality=dict(session_id='a'*32,origin_session_id='a'*32,
                     current_session_finalized=False,latest_event_sequence=1))),
                     response('events',dict(page=dict(origin_session_id='a'*32,latest_sequence=1,
                     events=[dict(sequence=1)],next_cursor=cursor))),
                     response('events',dict(page=dict(origin_session_id='a'*32,events=[]))),
                     response('session-summary',dict(available=False,summary=None,finalized_sessions=[]))]
        runner = acceptance.Runner(SimpleNamespace(workspace=Path('/unused')))
        with patch.object(runner, 'http', side_effect=responses) as http:
            runner.verify_quality(context, bindings)
        paths = [call.args[0] for call in http.call_args_list]
        self.assertTrue(paths[0].startswith('/api/quality/current?'))
        self.assertIn('cursor=%7B', paths[2])
        self.assertTrue(paths[3].startswith('/api/quality/session-summary?'))
        stale = response('current', {})
        stale['snapshot_id'] = 'f'*64
        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'RUNTIME_BINDING'):
            acceptance.quality_binding(stale,'cam_a',bindings['cam_a'],'generation','current')

    def test_strong_protect_token_never_written_to_logs(self):
        with tempfile.TemporaryDirectory() as root:
            runner = acceptance.Runner(SimpleNamespace(workspace=Path(root),manager_binary=Path('/fake')))
            runner.logs.mkdir()
            result = SimpleNamespace(returncode=0,stdout='{"token":"secret-memory-only"}',stderr='')
            with patch.object(acceptance.subprocess,'run',return_value=result):
                self.assertEqual(runner.cli('protect','protect','--require-strong')['token'],'secret-memory-only')
            self.assertEqual(list(runner.logs.iterdir()), [])
