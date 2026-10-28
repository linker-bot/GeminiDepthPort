"""Offline benchmark safety/measurement regressions; never invokes real Docker."""
import contextlib
import importlib.util
import io
import json
import shutil
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.validation import benchmark_services as runner
from scripts.validation import resource_monitor


def synthetic_spec():
    # Reuse the public artificial wire-contract fixture, never private inputs.
    location = Path(__file__).with_name('test_rgbd_acceptance.py')
    spec = importlib.util.spec_from_file_location('benchmark_fixture', location)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    value = fixture.spec()
    for stream in value['streams'].values():
        stream.update(width=640, height=480)
    return value


class BenchmarkServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        spec = synthetic_spec()
        self.config = {
            'color': {'width':640, 'height':480, 'fps':30, 'frame_id':spec['streams']['color']['frame_id']},
            'depth': {'width':640, 'height':480, 'fps':30, 'frame_id':spec['streams']['depth']['frame_id'], 'unit_policy':'normalize-mm'},
            'pairing': {'clock':'device', 'max_delta_us':20000},
            'ros': {'stamp_source':'sdk-system-unix', 'namespace':'/synthetic', 'bridge':'/bridge.so'},
            'sdk_library':'/sdk.so', 'runtime_contract':{'sdk_sha256':'a'*64, 'bridge_sha256':'b'*64},
            'startup_discovery_seconds':2}
        (self.root/'capture.json').write_text(json.dumps(self.config))
        (self.root/'runtime.json').write_text('{}')
        (self.root/'spec.json').write_text(json.dumps(spec))
        (self.root/'reference').write_bytes(b'synthetic-not-executable')
        self.calls = []
        self.clock = 0.
        self.samples = []
        self.hashes = ['a'*64, 'b'*64, 'c'*64]
        self.foreign = False
        self.fail_consumer = False
        self.official_loaded_hash = None

    def official_fixture(self):
        spec = json.loads((self.root/'spec.json').read_text())
        self.config['serial'] = 'synthetic-device'
        for role in ('color', 'depth'):
            frame = 'synthetic_camera_'+role+'_optical_frame'
            self.config[role]['frame_id'] = frame
            spec['streams'][role]['frame_id'] = frame
        spec['tf']['parent_frame'] = self.config['color']['frame_id']
        spec['tf']['child_frame'] = self.config['depth']['frame_id']
        (self.root/'capture.json').write_text(json.dumps(self.config))
        (self.root/'spec.json').write_text(json.dumps(spec))
        params = {key: False for key in runner.OFFICIAL_DISABLED_FLAGS}
        params.update(serial_number='synthetic-device', camera_name='synthetic_camera',
                      enable_color=True, enable_depth=True, color_format='YUYV', depth_format='Y16',
                      time_domain='system', enable_frame_sync=True, frame_aggregate_mode='ANY',
                      publish_tf=True, device_num=1)
        for role in ('color', 'depth'):
            for key in ('width', 'height', 'fps'):
                params[role+'_'+key] = self.config[role][key]
            params[role+'_qos'] = params[role+'_camera_info_qos'] = 'DEFAULT'
        (self.root/'official-params.json').write_text(json.dumps(params))
        return params

    def arguments(self, execute=False, release=False, official=False):
        result = ['benchmark', '--image','synthetic', '--cpp-binary',str(self.root/'reference'),
                  '--capture-config',str(self.root/'capture.json'), '--observer-spec',str(self.root/'spec.json'),
                  '--output-dir',str(self.root/'output'), '--seconds','40', '--trials','4',
                  '--device','/dev/bus/usb/003/002', '--domain','49']
        if execute: result.append('--execute')
        if release: result.append('--devices-released')
        if official: result += ['--official-params', str(self.root/'official-params.json')]
        return result

    def fake_command(self, args, timeout=20):
        self.calls.append(args)
        if args[:3] == ['docker','ps','-q']:
            return 'foreign-id' if self.foreign else ''
        if args[:3] == ['docker','image','inspect']:
            return json.dumps([{'Id':'sha256:synthetic', 'Config':{'User':'1000:1000'}}])
        if args[:2] == ['docker','inspect']:
            return json.dumps([{'Name':'/gdp-service-unrelated', 'State':{'Running':True}}])
        if 'ldd' in args: return 'libOrbbecSDK.so => /sdk.so (0x000)'
        if args[:2] == ['docker', 'exec'] and 'sdk_loaded_hashes' in args[-1]:
            return json.dumps({'sdk_loaded_hashes': [self.official_loaded_hash or self.hashes[0]]})
        if '-c' in args:
            return json.dumps({'hashes':self.hashes, 'machine':'synthetic', 'kernel':'synthetic'})
        if '/bench/observer.py' in args:
            if self.fail_consumer: raise ValueError('SYNTHETIC_FAILURE')
            stage = Path(next(v[:-10] for v in args if v.endswith(':/bench:rw')))
            (stage/'ready').write_text('ready')
            (stage/'observer-summary.json').write_text(json.dumps({
                'ok':True, 'mode':'standard-wire', 'streams':{
                    role:{'images':1200, 'matched_frames':1200, 'active_fps':30,
                          'age_ms':{'p99_bucket_upper_bound':10}} for role in ('color','depth')}}))
        return ''

    def run_runner(self, execute=False, release=False, official=False):
        real_module, real_exists = runner.module, Path.exists
        def read_group(group):
            # Sampling may begin only after discovery emitted ready, before go.
            if not self.samples:
                self.assertTrue((self.root/'output/trial-1/ready').exists())
                self.assertFalse((self.root/'output/trial-1/go').exists())
            self.samples.append(self.clock)
            return {'cpu_usec':int(self.clock*1e6), 'memory_bytes':100,
                    'oom_kill':0, 'memory_peak_bytes':100}
        monitor = SimpleNamespace(locate=lambda name:name, read_group=read_group,
                                  summarize=resource_monitor.summarize)
        def sleep(seconds): self.clock += seconds
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', self.arguments(execute, release, official)))
            stack.enter_context(patch.object(runner, 'command', self.fake_command))
            stack.enter_context(patch.object(runner.os, 'geteuid', return_value=0))
            stack.enter_context(patch.object(runner.os, 'chown'))
            stack.enter_context(patch.object(runner.time, 'monotonic', side_effect=lambda:self.clock))
            stack.enter_context(patch.object(runner.time, 'sleep', side_effect=sleep))
            stack.enter_context(patch.object(runner, 'module', side_effect=lambda path,name:monitor if name=='bench_resource' else real_module(path,name)))
            stack.enter_context(patch.object(Path, 'exists', lambda path:True if str(path)=='/dev/bus/usb/003/002' else real_exists(path)))
            stack.enter_context(contextlib.redirect_stdout(output))
            code = runner.main()
        return code, json.loads(output.getvalue())

    def test_plan_is_side_effect_free_and_declares_scope(self):
        code, report = self.run_runner()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root/'output').exists())
        self.assertFalse(report['hardware_executed'])
        self.assertFalse(report['pure_language_attribution'])
        self.assertEqual(report['plan']['min_frames'], 1000)
        self.assertEqual(report['plan']['order'], ['cpp','rust','rust','cpp'])
        compile(runner.observer_source(), '<benchmark-observer>', 'exec')

    def test_execution_requires_explicit_release_before_docker(self):
        code, report = self.run_runner(execute=True)
        self.assertEqual(code, 1)
        self.assertIn('EXPLICIT_RELEASE_AND_ROOT_REQUIRED', report['errors'])
        self.assertEqual(self.calls, [])

    def test_actual_library_mismatch_never_starts_producer(self):
        self.hashes[0] = 'd'*64
        code, report = self.run_runner(execute=True, release=True)
        self.assertEqual(code, 1)
        self.assertIn('ACTUAL_LIBRARY_HASH_MISMATCH', report['errors'])
        self.assertFalse(any('--device' in call for call in self.calls))
        self.assertFalse(report['hardware_executed'])

    def test_alternating_trials_gate_and_window_are_preserved(self):
        code, report = self.run_runner(execute=True, release=True)
        self.assertEqual(code, 0)
        self.assertEqual([trial['implementation'] for trial in report['trials']], ['cpp','rust','rust','cpp'])
        for trial in report['trials']:
            self.assertEqual(trial['resources']['elapsed_seconds'], 40)
            self.assertEqual(len(trial['resources']['results']), 2)
            for resource in trial['resources']['results']:
                self.assertEqual(resource['cpu_cores'], 1)
        producer_calls = [call for call in self.calls if '--device' in call]
        self.assertEqual(len(producer_calls), 4)
        for call in producer_calls:
            self.assertIn('--read-only', call)
            self.assertEqual(call[call.index('--ipc')+1], 'private')
            self.assertIn('ROS_LOCALHOST_ONLY=0', call)
        self.assertTrue(report['comparable_hashes_verified'])

    def test_existing_workload_is_never_stopped(self):
        self.foreign = True
        code, report = self.run_runner(execute=True, release=True)
        self.assertEqual(code, 1)
        self.assertIn('EXISTING_TEST_WORKLOAD', report['errors'])
        self.assertFalse(any(call[:3] == ['docker','rm','-f'] for call in self.calls))

    def test_failure_cleans_only_its_unique_names(self):
        self.fail_consumer = True
        code, report = self.run_runner(execute=True, release=True)
        self.assertEqual(code, 1)
        self.assertFalse(report['ok'])
        removed = [name for call in self.calls if call[:3] == ['docker','rm','-f'] for name in call[3:]]
        self.assertTrue(removed)
        created = {call[call.index('--name')+1] for call in self.calls if '--name' in call}
        self.assertTrue(set(removed).issubset(created))
        self.assertTrue(all(name.startswith('gdp-bench-') for name in removed))
        self.assertTrue((self.root/'output/trial-1/producer-private.log').exists())

    def test_existing_output_is_not_overwritten(self):
        (self.root/'output').mkdir()
        sentinel = self.root/'output/summary.json'
        sentinel.write_text('belongs-to-other-run')
        code, report = self.run_runner(execute=True, release=True)
        self.assertEqual(code, 1)
        self.assertIn('OUTPUT_EXISTS', report['errors'])
        self.assertEqual(sentinel.read_text(), 'belongs-to-other-run')
        self.assertEqual(self.calls, [])

    def test_official_plan_selects_mode_without_reading_unused_cpp_binary(self):
        self.official_fixture()
        (self.root/'reference').unlink()
        code, report = self.run_runner(official=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(report['plan']['order'], ['official', 'rust', 'rust', 'official'])
        self.assertTrue(report['official_driver_selected'])
        self.assertFalse(report['official_driver_compared'])
        self.assertFalse(report['pure_language_attribution'])
        self.assertFalse(report['plan']['internal_processing_identical'])
        self.assertFalse(report['plan']['bridge_used_by_official'])
        self.assertIsNone(report['plan']['cpp_binary_sha256'])
        self.assertTrue(report['plan']['official_initialization_differences'])
        compile(runner.OFFICIAL_LAUNCH_SOURCE, '<official-launch>', 'exec')

    def test_official_params_mismatches_are_rejected_before_any_docker_call(self):
        original = self.official_fixture()
        for key, value, error in (
                ('serial_number', 'different-device', 'OFFICIAL_SERIAL_MISMATCH'),
                ('camera_name', 'different_camera', 'OFFICIAL_FRAME_PREFIX_MISMATCH'),
                ('namespace', '/different', 'OFFICIAL_NAMESPACE_MISMATCH'),
                ('color_width', 1280, 'OFFICIAL_PROFILE_MISMATCH'),
                ('enable_temporal_filter', True, 'OFFICIAL_RAW_FILTER_POLICY_REQUIRED'),
                ('enable_frame_sync', False, 'OFFICIAL_SYNC_OR_TIME_MISMATCH')):
            with self.subTest(key=key):
                params = dict(original, **{key: value})
                (self.root/'official-params.json').write_text(json.dumps(params))
                code, report = self.run_runner(official=True)
                self.assertEqual(code, 1)
                self.assertIn(error, report['errors'])
                self.assertEqual(self.calls, [])

    def test_official_uses_actual_sdk_hash_then_same_gated_consumer_and_window(self):
        self.official_fixture()
        (self.root/'reference').unlink()
        self.official_loaded_hash = 'f'*64
        code, report = self.run_runner(execute=True, release=True, official=True)
        self.assertEqual(code, 1)
        self.assertIn('OFFICIAL_ACTUAL_SDK_HASH_MISMATCH', report['errors'])
        self.assertTrue(report['hardware_executed'])
        self.assertFalse(any('/bench/observer.py' in call for call in self.calls))
        shutil.rmtree(self.root/'output')
        self.calls, self.clock, self.samples = [], 0., []
        self.official_loaded_hash = None
        code, report = self.run_runner(execute=True, release=True, official=True)
        self.assertEqual(code, 0)
        self.assertEqual([trial['implementation'] for trial in report['trials']], ['official', 'rust', 'rust', 'official'])
        self.assertTrue(report['official_driver_compared'])
        self.assertTrue(report['comparable_hashes_verified'])
        self.assertFalse(any('ldd' in call for call in self.calls))
        self.assertEqual(len([call for call in self.calls if '--device' in call]), 4)
        for trial in report['trials']:
            self.assertEqual(trial['resources']['elapsed_seconds'], 40)
        stage = self.root/'output/trial-1'
        self.assertFalse((stage/'reference').exists())
        self.assertEqual((stage/'config/official_params.json').read_bytes(), (self.root/'official-params.json').read_bytes())
        self.assertEqual((stage/'config/official_launch.py').read_bytes(), runner.OFFICIAL_LAUNCH_SOURCE)


if __name__ == '__main__': unittest.main()
