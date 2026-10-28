"""Bundle/lifecycle integration with a synthetic Docker facade, never real devices."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import cameractl as ctl
import generation
import lifecycle
import operation_guard as guard
import web_server as web
from test_configuration_v2 import fixture, confirmed


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.data = confirmed(fixture())
        self.patches = [patch.object(ctl, 'ROOT', self.root), patch.object(ctl, 'INV', self.root/'inventory.json'),
                        patch.object(ctl, 'validate_bundle'), patch.object(ctl, 'assert_stopped')]
        for p in self.patches: p.start()
        ctl._atomic_inventory(self.data)

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def bundle(self, draft=False): return ctl.render(self.data, draft=draft)


class GenerationTests(Fixture):
    def test_three_services_preserve_explicit_namespace_and_yuyv(self):
        camera = self.data['cameras'][0]
        camera.update(namespace='/synthetic/custom', frame_prefix='synthetic_optical')
        self.data = confirmed(self.data); ctl._atomic_inventory(self.data)
        record = self.bundle()
        services = yaml.safe_load((record['folder']/'compose.yaml').read_text())['services']
        self.assertEqual(set(services), {'web', 'camera_camera_a', 'camera_camera_b', 'camera_camera_c'})
        for index, item in enumerate(generation.cameras(self.data)):
            name = item['name']; folder = record['folder']/'config'/name
            params = yaml.safe_load((folder/'camera.yaml').read_text())
            health = json.loads((folder/'health.json').read_text())
            binding = json.loads((folder/'binding.json').read_text())
            self.assertEqual(params['color_format'], 'YUYV')
            for key in ('color_qos', 'depth_qos', 'color_camera_info_qos', 'depth_camera_info_qos'):
                self.assertEqual(params[key], 'DEFAULT')
            self.assertEqual(health['streams'][0]['required_reliability'], 'reliable')
            self.assertEqual(health['streams'][0]['camera_info_topic'], item['namespace']+'/color/camera_info')
            self.assertEqual(params['camera_name'], item['frame_prefix'])
            self.assertEqual(params['device_num'], 3)
            self.assertFalse(params['depth_registration'])
            self.assertEqual(health['streams'][0]['topic'], item['namespace']+'/color/image_raw')
            self.assertEqual(health['streams'][0]['encoding'], 'yuyv')
            self.assertEqual(health['streams'][1]['encoding'], '16UC1')
            self.assertEqual(binding['namespace'], item['namespace'])
            service = services['camera_'+name]
            self.assertEqual(service['environment']['CAMERA_NAMESPACE'], item['namespace'])
            self.assertEqual(service['restart'], 'no')
            self.assertEqual(service['ipc'], 'private')
            self.assertEqual(service['labels']['orbbec.generation'], record['generation'])
            self.assertEqual(service['volumes'][0]['source'], str(folder))
            if index:
                self.assertEqual(service['depends_on'], {'camera_'+generation.cameras(self.data)[index-1]['name']:
                                                        {'condition':'service_healthy'}})
        ctl.check_render(self.data)

    def test_disabled_device_and_stream_not_generated_or_probed(self):
        self.data['cameras'][1]['enabled'] = False
        self.data['cameras'][0]['parameters']['enable_depth'] = False
        self.data = confirmed(self.data); ctl._atomic_inventory(self.data)
        record = self.bundle()
        self.assertEqual(len(record['cameras']), 2)
        self.assertFalse((record['folder']/'config/camera_b').exists())
        health = json.loads((record['folder']/'config/camera_a/health.json').read_text())
        self.assertEqual(len(health['streams']), 1)
        params = yaml.safe_load((record['folder']/'config/camera_c/camera.yaml').read_text())
        self.assertEqual(params['device_num'], 2)

    def test_zero_enabled_draft_is_management_only_and_cannot_start(self):
        for c in self.data['cameras']: c['enabled'] = False
        self.data = confirmed(self.data); ctl._atomic_inventory(self.data)
        record = self.bundle(draft=True)
        self.assertEqual(record['cameras'], [])
        services = yaml.safe_load((record['folder']/'compose.yaml').read_text())['services']
        self.assertEqual(set(services), {'web'})
        with self.assertRaises(ctl.ConfigurationError): ctl.start_cameras(self.data)

    def test_repeated_generation_reuses_files_and_pointer(self):
        first = self.bundle(); pointer = (self.root/'state/current-generation.json').read_bytes()
        with patch.object(ctl, 'assert_stopped', side_effect=AssertionError('must not stop')):
            second = self.bundle()
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual((self.root/'state/current-generation.json').read_bytes(), pointer)

    def test_files_and_generation_are_private(self):
        record = self.bundle()
        self.assertEqual(record['folder'].stat().st_mode & 0o777, 0o700)
        for path in record['folder'].rglob('*'):
            if path.is_file(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_compose_validation_keeps_previous_generation_and_user_files(self):
        previous = self.bundle(); pointer = (self.root/'state/current-generation.json').read_bytes()
        user = self.root/'config/unmanaged.txt'; user.parent.mkdir(); user.write_text('synthetic-user-file')
        self.data['image'] += '-synthetic'; self.data = confirmed(self.data); ctl._atomic_inventory(self.data)
        with patch.object(ctl, 'validate_bundle', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError): self.bundle()
        self.assertEqual((self.root/'state/current-generation.json').read_bytes(), pointer)
        self.assertTrue(previous['folder'].exists()); self.assertEqual(user.read_text(), 'synthetic-user-file')
        self.assertFalse(list((self.root/'state/generations').glob('.staging-*')))

    def test_pointer_failure_does_not_switch_or_leave_unowned_partial_bundle(self):
        previous = self.bundle(); pointer = (self.root/'state/current-generation.json').read_bytes()
        self.data['image'] += '-synthetic'; self.data = confirmed(self.data); ctl._atomic_inventory(self.data)
        with patch.object(generation, 'atomic_json', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError): self.bundle()
        self.assertEqual((self.root/'state/current-generation.json').read_bytes(), pointer)
        self.assertEqual(list((self.root/'state/generations').iterdir()), [previous['folder']])

    def test_source_drift_during_generation_never_activates(self):
        def drift(_): ctl.INV.write_bytes(ctl.INV.read_bytes()+b'\n')
        with patch.object(ctl, 'validate_bundle', side_effect=drift):
            with self.assertRaises(ctl.ConfigurationError): self.bundle()
        self.assertFalse((self.root/'state/current-generation.json').exists())

    def test_generated_file_drift_is_rejected(self):
        record = self.bundle(); (record['folder']/'config/camera_a/camera.yaml').write_text('synthetic drift')
        with self.assertRaises(ctl.ConfigurationError): ctl.check_render(self.data)

    def test_pointer_cannot_escape_managed_directory(self):
        self.bundle()
        guard.atomic_json(self.root/'state/current-generation.json', {'generation':'../outside','manifest_sha256':'synthetic'})
        with self.assertRaises(ctl.ConfigurationError): generation.load_current(self.root)

    def test_unconfirmed_or_mismatched_inventory_cannot_generate_formal_bundle(self):
        self.data['cameras'][0]['profiles_verified'] = False
        self.data['cameras'][0]['verification'] = None
        ctl._atomic_inventory(self.data)
        with self.assertRaises(ctl.ConfigurationError): self.bundle()
        data = confirmed(self.data)
        with self.assertRaises(ctl.ConfigurationError): ctl.render(data)
        self.assertFalse((self.root/'state/current-generation.json').exists())

    def test_unsupported_communication_profile_rejected_without_fallback(self):
        self.data['communication'] = {'profile':'unsupported-synthetic-profile'}
        with self.assertRaises(ctl.ConfigurationError): self.bundle()

    def test_compose_uses_immutable_generation_even_when_inventory_changes(self):
        record = self.bundle(); self.data['cameras'].pop(); ctl._atomic_inventory(self.data)
        with patch.object(ctl, 'run', return_value='') as command:
            ctl.compose('ps', '-a')
        self.assertIn(str(record['folder']/'compose.yaml'), command.call_args.args[0])
        with self.assertRaises(ctl.ConfigurationError): ctl.check_render(self.data)


class Docker:
    """Minimal stateful facade; any unexpected command fails the test."""
    def __init__(self, root, data, record):
        self.root=root;self.data=data;self.record=record;self.items={};self.events=[];self.fail_service=None;self.fail_recheck=None

    def container(self, name, generation_id=None, kind='camera', running=True):
        identifier = format(len(self.items)+1,'064x')
        labels={'com.docker.compose.project':ctl.PROJECT,'com.docker.compose.oneoff':'False',
                'orbbec.role':kind,'orbbec.camera':name,'orbbec.generation':generation_id or self.record['generation']}
        item={'Id':identifier,'Config':{'Labels':labels},'State':{'Running':running,'Status':'running' if running else 'exited',
                                                             'Health':{'Status':'healthy'}}}
        self.items[identifier]=item
        return item

    def instances(self, ctl_view, kind='camera'):
        if kind=='preview':return [copy.deepcopy(v) for v in self.items.values() if v['Config']['Labels'].get('orbbec.web.preview')==ctl.PROJECT]
        return [copy.deepcopy(v) for v in self.items.values() if v['Config']['Labels'].get('orbbec.role')==kind]

    def run(self, args, capture=False):
        self.events.append(tuple(args))
        if args[:2]==['docker','stop']:
            self.items[args[-1]]['State'].update(Running=False,Status='exited');return ''
        if args[:2]==['docker','rm']:
            del self.items[args[-1]];return ''
        raise AssertionError('Unexpected synthetic Docker command')

    def compose(self,*args,**kwargs):
        self.events.append(args)
        if args[0]=='run':return ''
        if args[0]=='up':
            service=args[-1]
            if service==self.fail_service:raise RuntimeError('PRIVATE_SENTINEL')
            self.container(service.removeprefix('camera_') if hasattr(str,'removeprefix') else service[7:]);return ''
        if args[0]=='exec':
            service=args[2]
            if service==self.fail_recheck:raise RuntimeError('PRIVATE_SENTINEL')
            return ''
        raise AssertionError('Unexpected synthetic Compose command')


class LifecycleTests(Fixture):
    def setUp(self):
        super().setUp();self.record=self.bundle();self.docker=Docker(self.root,self.data,self.record)
        self.runtime_patches=[patch.object(ctl,'preflight'),patch.object(ctl,'healthy',side_effect=self.healthy),
                              patch.object(lifecycle,'instances',side_effect=self.docker.instances),
                              patch.object(ctl,'compose',side_effect=self.docker.compose),patch.object(ctl,'run',side_effect=self.docker.run)]
        for p in self.runtime_patches:p.start()

    def tearDown(self):
        for p in reversed(self.runtime_patches):p.stop()
        super().tearDown()

    def healthy(self,service,timeout):self.docker.events.append(('healthy',service))

    def test_three_cameras_start_sequentially_after_actual_checks_then_group_recheck(self):
        result=ctl.start_cameras(self.data)
        self.assertTrue(result['ready']);self.assertFalse(result['reused'])
        events=self.docker.events
        for current,next_camera in zip(self.record['cameras'],self.record['cameras'][1:]):
            probe=('exec','-T',current['service'],'/app/entrypoint.sh','python3','/app/health.py')
            up=('up','-d','--no-deps','--no-build',next_camera['service'])
            self.assertLess(events.index(probe),events.index(up))
        self.assertEqual(sum(e[0]=='exec' for e in events),6)

    def test_second_camera_failure_preserves_partial_results_and_first_driver(self):
        self.docker.fail_service=self.record['cameras'][1]['service']
        with self.assertRaises(lifecycle.LifecycleError) as exc:ctl.start_cameras(self.data)
        result=exc.exception.result
        self.assertFalse(result['ready']);self.assertEqual(result['cameras'][0]['status'],'healthy')
        self.assertEqual(result['cameras'][1]['status'],'failed');self.assertEqual(result['cameras'][2]['status'],'not_started')
        self.assertEqual(len(self.docker.items),1)
        self.assertFalse(any(e[:2]==('docker','stop') for e in self.docker.events))
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))
        self.assertEqual(json.loads((self.root/'state/lifecycle.json').read_text())['status'],'failed')

    def test_same_healthy_generation_repeated_start_never_stops_or_recreates(self):
        ctl.start_cameras(self.data);self.docker.events.clear()
        result=ctl.ensure_started(self.data)
        self.assertTrue(result['ready']);self.assertTrue(result['reused'])
        self.assertFalse(any(e[0] in ('up','run') or e[:2]==('docker','stop') for e in self.docker.events))

    def test_existing_unhealthy_camera_requires_explicit_recovery(self):
        item=self.docker.container('camera_a');item['State']['Health']['Status']='unhealthy'
        with self.assertRaises(lifecycle.LifecycleError):ctl.start_cameras(self.data)
        self.assertFalse(any(e[0]=='up' for e in self.docker.events))

    def test_unknown_running_generation_is_not_implicitly_replaced(self):
        self.docker.container('orphan',generation_id='0'*32)
        with self.assertRaises(lifecycle.LifecycleError) as exc:ctl.start_cameras(self.data)
        self.assertEqual(exc.exception.code,'RUNNING_CONFIGURATION_CHANGED')
        self.assertFalse(any(e[0]=='up' for e in self.docker.events))

    def test_duplicate_running_identity_rejected(self):
        self.docker.container('camera_a');self.docker.container('camera_a')
        with self.assertRaises(lifecycle.LifecycleError) as exc:ctl.start_cameras(self.data)
        self.assertEqual(exc.exception.code,'DUPLICATE_INSTANCE')

    def test_final_probe_failure_never_reports_group_ready(self):
        calls=[]
        original=self.docker.compose
        def fail(*args,**kwargs):
            if args[0]=='exec':
                calls.append(args[2])
                if len(calls)==4:raise ValueError('PRIVATE_SENTINEL')
            return original(*args,**kwargs)
        with patch.object(ctl,'compose',side_effect=fail):
            with self.assertRaises(lifecycle.LifecycleError) as exc:ctl.start_cameras(self.data)
        self.assertFalse(exc.exception.result['ready'])
        self.assertEqual(exc.exception.result['cameras'][0]['status'],'failed')

    def test_stop_uses_discovered_instances_after_device_removed_from_inventory(self):
        orphan=self.docker.container('removed_camera',generation_id='0'*32)
        management=self.docker.container('web',kind='management')
        self.data['cameras']=[];ctl._atomic_inventory(self.data)
        result=ctl.stop_cameras()
        self.assertEqual(result['status'],'stopped');self.assertFalse(self.docker.items[orphan['Id']]['State']['Running'])
        self.assertTrue(self.docker.items[management['Id']]['State']['Running'])
        self.docker.events.clear();ctl.stop_cameras()
        self.assertFalse(any(e[:2]==('docker','stop') for e in self.docker.events))

    def test_stop_continues_other_devices_after_one_failure_and_keeps_management(self):
        a=self.docker.container('camera_a');b=self.docker.container('camera_b')
        manager=self.docker.container('web',kind='management')
        original=self.docker.run
        def run(args,capture=False):
            if args[:2]==['docker','stop'] and args[-1]==a['Id']:raise RuntimeError('PRIVATE_SENTINEL')
            return original(args,capture)
        with patch.object(ctl,'run',side_effect=run):
            with self.assertRaises(lifecycle.LifecycleError):ctl.stop_stack()
        self.assertFalse(self.docker.items[b['Id']]['State']['Running'])
        self.assertTrue(self.docker.items[manager['Id']]['State']['Running'])

    def test_recording_guard_blocks_all_mutations_and_survives_manager_restart(self):
        ctl.start_cameras(self.data);token=ctl.acquire_recording_guard();self.docker.events.clear()
        for operation in (lambda:ctl.save(self.data),lambda:ctl.render(self.data),lambda:ctl.start_cameras(self.data),
                          lambda:ctl.stop_cameras(),lambda:ctl.stop_previews(),lambda:ctl.stop_stack(),
                          lambda:ctl.build_image(self.data),lambda:ctl.migrate_inventory(apply=True)):
            with self.assertRaises(ctl.ConfigurationError) as exc:operation()
            self.assertEqual(exc.exception.code,'RECORDING_PROTECTED')
        with patch.object(web,'ROOT',self.root):
            manager=web.Manager()
            for operation in (lambda:manager.start_job('up'),lambda:manager.start_preview('head'),
                              lambda:manager.stop_preview('head'),lambda:manager.save_config({}),lambda:manager.confirm('head')):
                with self.assertRaises(ctl.ConfigurationError):operation()
        self.assertEqual(self.docker.events,[])
        with self.assertRaises(ctl.ConfigurationError):ctl.release_recording_guard('wrong-synthetic-token')
        ctl.release_recording_guard(token);self.assertFalse(guard.protected(self.root))
        self.assertNotIn(token,(self.root/'state/lifecycle.json').read_text())

    def test_corrupt_guard_is_fail_closed_without_automatic_timeout(self):
        file=self.root/'state/recording-guard.json';file.write_text('synthetic-corrupt')
        with self.assertRaises(ctl.ConfigurationError):ctl.stop_cameras()
        with self.assertRaises(ctl.ConfigurationError):ctl.release_recording_guard('synthetic-token')
        self.assertTrue(file.exists())

    def test_recording_guard_requires_all_required_cameras(self):
        with self.assertRaises(ctl.ConfigurationError):ctl.acquire_recording_guard()
        self.assertFalse(guard.protected(self.root))


class OwnershipTests(unittest.TestCase):
    def test_discovery_does_not_open_devices_and_checks_returned_ownership(self):
        identifier='a'*64
        item={'Id':identifier,'Config':{'Labels':{'com.docker.compose.project':ctl.PROJECT,
              'com.docker.compose.oneoff':'False','orbbec.role':'camera'}},'State':{'Running':True}}
        with patch.object(ctl,'run',side_effect=[identifier,json.dumps([item])]) as run:
            self.assertEqual(lifecycle.instances(ctl.controller())[0]['Id'],identifier)
            self.assertTrue(all(call.args[0][1] in ('ps','inspect') for call in run.call_args_list))
        item['Config']['Labels']['orbbec.role']='unmanaged'
        with patch.object(ctl,'run',side_effect=[identifier,json.dumps([item])]):
            with self.assertRaises(ctl.ConfigurationError):lifecycle.instances(ctl.controller())

    def test_other_process_cannot_acquire_shared_lifecycle_lock(self):
        with tempfile.TemporaryDirectory() as root:
            with guard.control_lock(Path(root)):
                code='from pathlib import Path; import operation_guard as g; import sys\nwith g.control_lock(Path(sys.argv[1])): pass'
                result=subprocess.run([sys.executable,'-c',code,root],cwd=BASE,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0);self.assertIn('CONTROL_BUSY',result.stderr)

    def test_deploy_script_has_no_inventory_named_camera_or_unguarded_compose_down(self):
        script=(BASE/'deploy.sh').read_text()
        self.assertNotIn('camera_head',script);self.assertNotIn('camera_wrist',script)
        self.assertNotIn('down --remove-orphans',script)
        self.assertIn('cameractl.py stack-stop',script)
        self.assertIn('recording-guard.json',script)




class PreflightTests(Fixture):
    def setUp(self):
        super().setUp(); self.bundle()
        self.labels={'org.opencontainers.image.revision':self.data['upstream_commit'],
                     'org.geminidepthport.explicit-bindings':'1', 'org.geminidepthport.native-yuyv':'1',
                     'org.geminidepthport.native-yuyv-patch':ctl.driver_build.patch_identity()['patch_sha256'],
                     'org.geminidepthport.communication-profile':generation.COMMUNICATION_PROFILE}
        self.device_patches=[patch.object(ctl,'assert_no_previews'),patch.object(ctl.platform,'system',return_value='Linux'),
                             patch.object(ctl.Path,'is_dir',return_value=True),patch.object(ctl.Path,'is_file',return_value=True),
                             patch.object(ctl,'scan',return_value=[{'serial_number':c['serial_number']} for c in self.data['cameras']]),
                             patch.object(ctl,'compose',return_value='')]
        for p in self.device_patches:p.start()

    def tearDown(self):
        for p in reversed(self.device_patches):p.stop()
        super().tearDown()

    def test_old_image_without_native_yuyv_is_rejected(self):
        self.labels.pop('org.geminidepthport.native-yuyv')
        with patch.object(ctl,'run',side_effect=['',json.dumps([{'Config':{'Labels':self.labels}}])]):
            with self.assertRaises(ctl.ConfigurationError) as exc:ctl.preflight(self.data)
        self.assertEqual(exc.exception.code,'IMAGE_CONTRACT_UNSUPPORTED')

    def test_declared_image_contract_and_revision_pass_controlled_preflight(self):
        with patch.object(ctl,'run',side_effect=['',json.dumps([{'Config':{'Labels':self.labels}}])]):
            ctl.preflight(self.data)

    def test_native_marker_with_wrong_patch_identity_is_rejected(self):
        self.labels['org.geminidepthport.native-yuyv-patch']='synthetic-wrong-patch'
        with patch.object(ctl,'run',side_effect=['',json.dumps([{'Config':{'Labels':self.labels}}])]):
            with self.assertRaises(ctl.ConfigurationError):ctl.preflight(self.data)

    def test_missing_device_binding_is_rejected_without_echo(self):
        with patch.object(ctl,'scan',return_value=[]):
            with self.assertRaises(ctl.ConfigurationError) as exc:ctl.preflight(self.data)
        self.assertNotIn('SYNTHETIC_',str(exc.exception));self.assertEqual(exc.exception.code,'DEVICE_UNAVAILABLE')


class RuntimeBindingTests(unittest.TestCase):
    def test_namespace_and_frame_are_independently_passed_to_driver_description(self):
        from types import ModuleType
        actions=ModuleType('launch_ros.actions');descriptions=ModuleType('launch_ros.descriptions');launch=ModuleType('launch')
        actions.ComposableNodeContainer=lambda **kwargs:kwargs
        descriptions.ComposableNode=lambda **kwargs:kwargs
        launch.LaunchDescription=lambda nodes:nodes
        with patch.dict(sys.modules,{'launch':launch,'launch_ros.actions':actions,'launch_ros.descriptions':descriptions}):
            spec=importlib.util.spec_from_file_location('bound_launch',BASE/'runtime/launch_camera.py')
            # Loading the launch adapter must not require a running ROS graph.
            with patch.dict(sys.modules,{'validate_runtime':type('Validator',(),{'read_parameters':lambda:None,'launch_arguments':lambda value:[]})}):
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            params={'camera_name':'synthetic_optical','color_format':'YUYV'}
            result=module.explicit_description(params,'/synthetic/existing','logical_sensor')
        node=result[0]['composable_node_descriptions'][0]
        self.assertEqual(node['namespace'],'/synthetic/existing');self.assertEqual(node['parameters'],[params])
        self.assertEqual(node['name'],'synthetic_optical')
        self.assertEqual(node['remappings'],[('/diagnostics','/synthetic/existing/diagnostics')])
        self.assertEqual(result[0]['namespace'],'/synthetic/existing')


class HostShellGuardTests(unittest.TestCase):
    def test_host_wrapper_refuses_recording_guard_before_side_effects(self):
        # Extract the actual wrapper functions, without running host initialization.
        script=(BASE/'deploy.sh').read_text()
        functions=script[script.index('stack_lock() {'):script.index('install_rules() {')]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'state').mkdir();(root/'state/recording-guard.json').write_text('{}')
            program='set -e\nROOT="$1"\nfail() { exit 73; }\n'+functions+'\nstack_lock\nprintf unsafe > "$ROOT/unsafe"\n'
            # flock is a Linux deployment dependency; use the same lock behavior on hosts that provide it.
            import shutil
            if not shutil.which('flock'):self.skipTest('Host flock unavailable; Linux CI exercises wrapper')
            result=subprocess.run(['bash','-c',program,'wrapper',str(root)],capture_output=True,text=True)
            self.assertEqual(result.returncode,73);self.assertFalse((root/'unsafe').exists())


if __name__=='__main__':unittest.main()
