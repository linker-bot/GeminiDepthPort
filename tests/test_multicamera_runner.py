"""Synthetic runner lifecycle only. No real Docker, cameras, SSH or scale trial."""
import copy
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.testing import multicamera_runner as runner


def synthetic_spec():
    spec=importlib.util.spec_from_file_location('multicamera_synthetic_spec',Path(__file__).with_name('test_rgbd_acceptance.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.spec()


class FakeClock:
    def __init__(self, callback=None):self.now=0.;self.callback=callback
    def monotonic(self):return self.now
    def sleep(self, seconds):
        self.now+=seconds
        if self.callback:self.callback(self.now)


class FakeDocker:
    def __init__(self, rows, failure=None):
        self.rows={row['camera']['id']:row for row in rows};self.failure=failure
        self.containers={};self.calls=[];self.stopped=[];self.run_count=0
    def inspect(self,name):
        self.calls.append(('inspect',name))
        if name in self.containers:return copy.deepcopy(self.containers[name])
        if name in {r['camera']['image'] for r in self.rows.values()}:
            return {'Id':'sha256:'+'d'*64,'Config':{'Labels':{'org.geminidepthport.capture-backend':'rust-v1'}}}
        raise subprocess.CalledProcessError(1,['docker','inspect',name],stderr=b'No such object')
    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(runner.canonical(value));path.chmod(0o600)
    def run(self,args):
        self.calls.append(tuple(args))
        if args[:2]==['context','inspect']:
            host='tcp://remote.invalid:2375' if self.failure=='remote' else 'unix:///var/run/docker.sock'
            return runner.canonical([{'Endpoints':{'docker':{'Host':host}}}])
        if args[0]=='info':return runner.canonical(['name=rootless'] if self.failure=='rootless' else ['name=seccomp'])
        if args[0]=='ps':
            label=args[args.index('--filter')+1].removeprefix('label=')
            key,value=label.split('=',1)
            return ('\n'.join(n for n,v in self.containers.items() if v['Config']['Labels'].get(key)==value)+'\n').encode()
        if args[0]=='stop':
            self.stopped.append(args[-1]);self.containers[args[-1]]['State']['Running']=False;return b''
        if args[0]=='logs':
            if self.failure=='cleanup-log':raise OSError('synthetic log read failure')
            return b'synthetic container log\n'
        if args[0]!='run':raise AssertionError(args)
        assert 'max-file=1' in args and 'compress=false' in args, 'single-file local logs require explicit no-compression'
        self.run_count+=1
        name=args[args.index('--name')+1]
        labels=[args[i+1] for i,v in enumerate(args) if v=='--label']
        label_dict=dict(item.split('=',1) for item in labels)
        self.containers[name]={'Config':{'Labels':label_dict},'State':{'Running':True,'ExitCode':0}}
        mounts=[args[i+1] for i,v in enumerate(args) if v=='-v']
        if name.endswith('-consumer'):
            directory=Path(next(value.split(':')[0] for value in mounts if value.endswith(':/validation')))
            self.write(directory/'consumer-result.json',{'ok':self.failure!='consumer-result','matched_pairs':32,'contract_errors':{}})
            self.containers[name]['State'].update(Running=False,ExitCode=1 if self.failure=='consumer-exit' else 0)
            if self.failure=='consumer-owner':self.containers[name]['Config']['Labels'][runner.LABEL]='foreign-run'
        else:
            directory=Path(next(value.split(':')[0] for value in mounts if value.endswith(':/tmp/capture')))
            config=runner.parse((directory.parent/'capture.json').read_bytes())
            spec=runner.parse((directory.parent/'consumer.json').read_bytes())
            identity=runner.digest(name.encode());session=runner.digest((name+'session').encode())[:32]
            instance=runner.digest((name+'instance').encode())
            status={'state':'ready','snapshot_id':identity,'session_id':session,'instance_id':instance,
                    'generation':config['service']['generation'],'config_sha256':runner.digest((directory.parent/'capture.json').read_bytes())}
            if self.failure=='startup-not-ready':status['state']='opening'
            if self.failure=='generation':status['generation']='f'*32
            models={}
            for role in ('color','depth'):
                stream=spec['streams'][role];calibration=stream['calibration']
                models[role]={'frame_id':stream['frame_id'],'width':stream['width'],'height':stream['height'],
                              'k':calibration['k'],'distortion_model':calibration['distortion_model'],
                              'distortion_coefficients':calibration['d']}
            if self.failure=='calibration':models['depth']['k']=list(models['depth']['k']);models['depth']['k'][0]+=1.
            snapshot={'snapshot_id':identity,'content':{'binding':{'session_id':session,'instance_id':instance,'generation_id':status['generation']},
                'configuration':{'runtime_config_sha256':status['config_sha256']},'calibration':{'source':'sdk-active-profiles',
                'physical_accuracy_verified':False,'models':models,'transforms':[{'source_frame':spec['tf']['child_frame'],
                'target_frame':spec['tf']['parent_frame'],'rotation_row_major':[1,0,0,0,1,0,0,0,1],
                'translation_m':spec['tf']['translation_m']}]}}}
            self.write(directory/'status.json',status)
            self.write(directory/'snapshots'/(identity+'.json'),snapshot)
            if self.failure=='startup-dead':self.containers[name]['State']['Running']=False
            if self.failure=='producer-client-error':raise subprocess.TimeoutExpired(['docker','run'],60)
        if self.failure=='consumer-client-error' and name.endswith('-consumer'):
            raise subprocess.TimeoutExpired(['docker','run'],60)
        return b'synthetic-container-id\n'


class MultiCameraRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None)
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
    def private(self,path,value):
        path.write_bytes(runner.canonical(value));path.chmod(0o600);return path
    def manifest(self,count=4):
        runtime={key:'a'*64 for key in ('sdk_sha256','bridge_sha256','service_sha256')}
        cameras=[]
        for i in range(count):
            name=f'camera_{i}';namespace='/'+name;spec=synthetic_spec()
            for role in ('color','depth'):
                spec['streams'][role].update(image_topic=f'{namespace}/{role}/image_raw',camera_info_topic=f'{namespace}/{role}/camera_info',
                    metadata_topic=f'{namespace}/{role}/frame_metadata',frame_id=f'{name}_{role}_optical')
            spec['pair']['topic']=namespace+'/rgbd/pair_metadata'
            spec['tf'].update(parent_frame=spec['streams']['color']['frame_id'],child_frame=spec['streams']['depth']['frame_id'])
            config={'serial':f'synthetic-private-serial-{i}','color':{'width':2,'height':2,'fps':30,'frame_id':spec['streams']['color']['frame_id']},
                'depth':{'width':2,'height':2,'fps':30,'frame_id':spec['streams']['depth']['frame_id']},'ros':{'namespace':namespace},
                'service':{'generation':'b'*32,'status_path':'/tmp/capture/status.json','recording_control_path':'/recording/control.json','max_restarts':0,'startup_timeout_ms':1000},
                'pairing':{'clock':'device','max_delta_us':2000},'quality_policy':{'max_pair_delta_us':2000},
                'quality_runtime':{'quality':{'max_pair_delta_us':2000}},'runtime_contract':runtime}
            camera={'id':name,'image':'synthetic/capture:test'}
            for key,value in (('capture_config',config),('runtime_manifest',runtime),('consumer_spec',spec)):
                camera[key]=str(self.private(self.root/f'{name}-{key}.json',value))
            cameras.append(camera)
        return self.private(self.root/'manifest.json',{'version':1,'cameras':cameras,'seconds':2,'usbfs_min_mb':64})
    def run_simulated(self,failure=None,clock=None):
        description,rows=runner.plan(self.manifest())
        docker=FakeDocker(rows,failure)
        with patch.object(runner,'time',clock or FakeClock()):
            try:result=runner.execute(description,rows,self.root/'output',docker=docker)
            except Exception:result=runner.parse((self.root/'output/result.json').read_bytes())
        return result,docker
    def test_plan_four_has_no_docker_or_production_limit_change_and_redacts_inputs(self):
        path=self.manifest()
        with patch.object(subprocess,'check_output',side_effect=AssertionError('preflight must not invoke Docker')):
            description,rows=runner.plan(path)
        self.assertEqual(len(rows),4);self.assertEqual(description['production_camera_limit_unchanged'],3)
        self.assertFalse(description['execution_requested']);self.assertFalse(description['capacity_certified'])
        text=json.dumps(description);self.assertNotIn('synthetic-private-serial',text);self.assertNotIn(str(self.root),text)
        with patch.object(sys,'argv',['runner','--manifest',str(path)]),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(runner.main(),0)
        self.assertNotIn('synthetic-private-serial',out.getvalue())
    def test_strict_json_and_private_file_shape(self):
        for raw in (b'{"a":{"x":1,"x":2}}',b'{"a":NaN}',b'{"a":1e999}'):
            with self.assertRaises(runner.TestError):runner.parse(raw)
        path=self.private(self.root/'private.json',{})
        path.chmod(0o640)
        with self.assertRaises(runner.TestError):runner.private_read(path)
        path.chmod(0o600);alias=self.root/'alias.json';alias.symlink_to(path)
        with self.assertRaises(OSError):runner.private_read(alias)
        hard=self.root/'hard.json';os.link(path,hard)
        with self.assertRaises(runner.TestError):runner.private_read(path)
        hard.unlink();parent=self.root/'alias-parent';parent.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(OSError):runner.private_read(parent/path.name)
        with patch.object(runner.os,'geteuid',return_value=os.geteuid()+1):
            with self.assertRaises(runner.TestError):runner.private_read(path)
    def test_plan_rejects_duplicate_devices_namespaces_frames_runtime_and_generation(self):
        for index,mutate in enumerate((
            lambda c,s,r:c.update(serial='synthetic-private-serial-0'),
            lambda c,s,r:c['ros'].update(namespace='/camera_0/child'),
            lambda c,s,r:c['color'].update(frame_id='camera_0_color_optical'),
            lambda c,s,r:r.update(sdk_sha256='not-a-hash'),
            lambda c,s,r:c['service'].update(generation='f'*32),
            lambda c,s,r:s['streams']['depth'].update(metadata_topic='/foreign/depth'),
        )):
            case=self.root/f'case{index}';case.mkdir();old=self.root;self.root=case
            try:
                path=self.manifest();value=runner.parse(path.read_bytes());camera=value['cameras'][1]
                c=runner.parse(Path(camera['capture_config']).read_bytes());s=runner.parse(Path(camera['consumer_spec']).read_bytes());r=runner.parse(Path(camera['runtime_manifest']).read_bytes())
                mutate(c,s,r)
                self.private(Path(camera['capture_config']),c);self.private(Path(camera['consumer_spec']),s);self.private(Path(camera['runtime_manifest']),r)
                with self.assertRaises(runner.TestError):runner.plan(path)
            finally:self.root=old
    def test_full_four_camera_fake_lifecycle_has_true_simulation_and_owned_cleanup(self):
        result,docker=self.run_simulated()
        self.assertTrue(result['ok'],result);self.assertTrue(result['simulated']);self.assertEqual(len(result['cameras']),4)
        self.assertEqual(len(docker.stopped),4);self.assertTrue(all(not v['State']['Running'] for v in docker.containers.values()));self.assertTrue((self.root/'output/COMPLETE').exists())
        self.assertEqual(result['capture_window_seconds'],2)
        self.assertTrue(all(name.startswith('gdp-test-') for name in docker.stopped))
        self.assertNotIn('synthetic-private-serial',json.dumps(result))
    def test_failed_consumers_or_capture_never_have_complete(self):
        for failure in ('consumer-result','consumer-exit','startup-dead','startup-not-ready','generation','calibration','cleanup-log','consumer-owner'):
            with self.subTest(failure=failure):
                case=self.root/failure;case.mkdir();old=self.root;self.root=case
                try:
                    result,docker=self.run_simulated(failure)
                    self.assertFalse(result['ok'],result);self.assertFalse((case/'output/COMPLETE').exists())
                    self.assertTrue(result['simulated'])
                    if failure=='consumer-owner':
                        self.assertTrue(all(docker.containers[name]['Config']['Labels'][runner.LABEL]!='foreign-run' for name in docker.stopped))
                finally:self.root=old
    def test_client_error_after_daemon_create_is_cleaned_for_both_roles(self):
        for failure in ('producer-client-error','consumer-client-error'):
            case=self.root/failure;case.mkdir();old=self.root;self.root=case
            try:
                result,docker=self.run_simulated(failure)
                self.assertFalse(result['ok']);self.assertTrue(docker.stopped);self.assertTrue(all(not v['State']['Running'] for v in docker.containers.values()))
            finally:self.root=old
    def test_remote_or_rootless_daemon_refused_before_container_operations(self):
        for failure in ('remote','rootless'):
            case=self.root/failure;case.mkdir();old=self.root;self.root=case
            try:
                description,rows=runner.plan(self.manifest());docker=FakeDocker(rows,failure)
                with self.assertRaises(runner.TestError):runner.execute(description,rows,case/'output',docker=docker)
                self.assertFalse(docker.containers);self.assertFalse((case/'output').exists())
            finally:self.root=old
    def test_changed_input_and_wrong_confirmation_never_start_containers(self):
        path=self.manifest();description,rows=runner.plan(path);docker=FakeDocker(rows)
        Path(rows[0]['camera']['capture_config']).write_bytes(b'{}')
        with self.assertRaises(runner.TestError):runner.execute(description,rows,self.root/'output',docker=docker)
        self.assertFalse(docker.containers)
        Path(rows[0]['camera']['capture_config']).write_bytes(rows[0]['inputs']['capture_config'])
        with patch.object(runner,'production_lock',side_effect=AssertionError('wrong confirmation must not lock or execute')),patch.object(sys,'argv',['runner','--manifest',str(path),'--execute','--confirm-plan','wrong','--output',str(self.root/'other')]),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(),1)
    def test_management_lock_same_inode_nonblocking_guard_and_links(self):
        management=self.root/'management';state=management/'state';state.mkdir(parents=True,mode=0o700)
        with runner.production_lock(management):
            inode=(state/'control.lock').stat().st_ino
            with self.assertRaisesRegex(runner.TestError,'MANAGEMENT_OPERATION_IN_PROGRESS'):
                with runner.production_lock(management):pass
        with runner.production_lock(management):self.assertEqual((state/'control.lock').stat().st_ino,inode)
        (state/'recording-guard.json').symlink_to(state/'missing-guard')
        with self.assertRaisesRegex(runner.TestError,'RECORDING_PROTECTED'):
            with runner.production_lock(management):pass
        (state/'recording-guard.json').unlink();(state/'control.lock').unlink()
        (state/'control.lock').symlink_to(self.private(self.root/'other-lock',{}))
        with self.assertRaises(OSError):
            with runner.production_lock(management):pass
    def test_real_execution_requires_root_before_docker_factory(self):
        description,rows=runner.plan(self.manifest())
        with patch.object(runner.os,'geteuid',return_value=12345),patch.object(runner,'Docker',side_effect=AssertionError('no Docker')):
            with self.assertRaisesRegex(runner.TestError,'ROOTFUL_TEST_EXECUTION_REQUIRED'):
                runner.execute(description,rows,self.root/'output')

    def test_mid_window_session_change_is_failure_not_completed_acceptance(self):
        description,rows=runner.plan(self.manifest());docker=FakeDocker(rows)
        def change(_):
            status=self.root/'output/camera_0/status/status.json'
            value=runner.parse(status.read_bytes());value['session_id']='f'*32
            self.private(status,value)
        with patch.object(runner,'time',FakeClock(change)):
            result=runner.execute(description,rows,self.root/'output',docker=docker)
        self.assertFalse(result['ok']);self.assertFalse(result['capture_observations_ok'])
        self.assertEqual(result['observation_exceptions'][0]['code'],'SESSION_CHANGED')
        self.assertFalse((self.root/'output/COMPLETE').exists());self.assertEqual(len(docker.stopped),4);self.assertTrue(all(not v['State']['Running'] for v in docker.containers.values()))

    def test_guard_appearing_mid_run_stops_only_own_containers(self):
        description,rows=runner.plan(self.manifest());docker=FakeDocker(rows);guard=self.root/'guard'
        clock=FakeClock(lambda _:guard.touch(exist_ok=True))
        with patch.object(runner,'time',clock):
            with self.assertRaisesRegex(runner.TestError,'RECORDING_PROTECTED'):
                runner.execute(description,rows,self.root/'output',docker=docker,guard_path=guard)
        result=runner.parse((self.root/'output/result.json').read_bytes());self.assertFalse(result['ok']);self.assertEqual(len(docker.stopped),4);self.assertTrue(all(not v['State']['Running'] for v in docker.containers.values()))
        self.assertTrue(guard.exists());self.assertFalse((self.root/'output/COMPLETE').exists())


if __name__=='__main__':unittest.main()
