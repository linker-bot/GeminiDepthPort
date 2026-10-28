"""Gate harness synthetic lifecycle: fake Docker/clock, real shared Rust verifier/release CLI."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as N
import unittest
from tools.validation import native_gate_acceptance as gate

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL=Path(os.environ.get('GDP_GATE_PROTOCOL_PROGRAM',ROOT/'target/debug/examples/gate_protocol'))
MANAGER=Path(os.environ.get('GDP_MANAGER_PROGRAM',ROOT/'target/debug/geminidepthport'))


class FakeClock:
    def __init__(self):self.now=0.;self.tick=lambda:None
    def monotonic(self):return self.now
    def sleep(self,seconds):self.now+=seconds;self.tick()


class FakeDocker:
    def __init__(self,protocol,ack_wrong_snapshot=False):
        self.protocol=protocol;self.ack_wrong_snapshot=ack_wrong_snapshot;self.calls=[];self.created=False;self.killed=False
        self.name=None;self.run_id=None;self.directory=None;self.gate=None;self.status=None;self.stopped=False
    def inspect(self,name):
        if name!=self.name:
            return {'Id':'sha256:'+'a'*64,'Config':{'Labels':{'org.geminidepthport.capture-backend':'rust-v1'}}}
        return {'Config':{'Labels':{gate.LABEL:self.run_id}},'State':{'Running':not self.stopped}}
    def write(self,path,value):gate.atomic(path,gate.canonical(value))
    def tick(self):
        if not self.gate or not (self.gate/'control.json').exists() or self.killed:return
        request=gate.parse((self.gate/'control.json').read_bytes())
        if request['binding']['session_id']!=self.status['session_id']:return
        if not self.ack_wrong_snapshot and request['snapshot_id']!=self.status['snapshot_id']:return
        result=subprocess.run([str(self.protocol),'request-digest',str(self.gate/'control.json')],capture_output=True,check=True)
        digest=gate.parse(result.stdout)['request_sha256']
        self.write(self.gate/'ack.json',dict(version=1,request_id=request['request_id'],request_sha256=digest,
                                           worker_pid=self.status['pid'],supervisor_pid=1,session_id=self.status['session_id']))
    def run(self,*args,input=None,timeout=60,check=True):
        self.calls.append(args)
        if args[:2]==('context','inspect'):return N(stdout=gate.canonical([{'Endpoints':{'docker':{'Host':'unix:///var/run/docker.sock'}}}]))
        if args[0]=='info':return N(stdout=b'["name=seccomp"]')
        if args[0]=='run':
            assert 'max-file=1' in args and 'compress=false' in args, 'single-file local logs require explicit no-compression'
            self.name=args[args.index('--name')+1];self.run_id=args[args.index('--label')+1].split('=',1)[1];self.created=True
            mounts=[args[i+1] for i,v in enumerate(args) if v=='-v']
            directory=Path(next(v.split(':')[0] for v in mounts if v.endswith(':/tmp/capture')))
            self.directory=directory;self.gate=Path(next(v.split(':')[0] for v in mounts if v.endswith(':/recording')))
            raw=(directory.parent/'runtime/capture.json').read_bytes();config=gate.parse(raw)
            self.status=dict(version=1,kind='capture-service-status',ready=True,state='ready',pid=42,
                session_id='b'*32,instance_id='c'*64,generation=config['service']['generation'],config_sha256=gate.sha(raw),
                snapshot_id='d'*64,pairs_published=10,restart_attempt=0)
            binding=dict(instance_id='c'*64,session_id='b'*32,generation_id=config['service']['generation'],
                         configuration_id='e'*64,calibration_id='f'*64)
            (directory/'snapshots').mkdir(mode=0o700)
            self.write(directory/'snapshots'/('d'*64+'.json'),dict(snapshot_id='d'*64,content=dict(binding=binding)))
            self.write(directory/'status.json',self.status);return N(stdout=b'fake-container-id')
        if args[0]=='exec':
            code=args[-1];request=gate.parse(input) if input else None
            if code==gate.PROBE_WORKER:
                if self.killed and not self.gate.joinpath('control.json').exists():
                    self.status.update(ready=True,state='ready',pid=43,session_id='a'*32,snapshot_id='9'*64,restart_attempt=1)
                    self.killed=False;self.write(self.directory/'status.json',self.status)
                return N(stdout=gate.canonical(dict(status=self.status,alive=not self.killed,start_ticks='100',parent_pid=1)))
            if code==gate.KILL_WORKER:
                self.killed=True;self.status.update(state='degraded',ready=False,reason='WORKER_EXITED')
                self.write(self.directory/'status.json',self.status);return N(stdout=b'{"killed":true}')
            if code==gate.BLOCKED_WINDOW:
                self.status.update(reason='RECORDING_PROTECTED_RECOVERY_BLOCKED')
                self.write(self.directory/'status.json',self.status)
                return N(stdout=gate.canonical(dict(elapsed_seconds=request['seconds'],samples=50,new_worker_observations=0,
                    session_change_observations=0,blocked_reason_observed=True,last_status=self.status)))
            raise AssertionError('unexpected embedded probe')
        if args[0]=='stop':self.stopped=True;return N(stdout=b'fake-stopped')
        if args[0]=='logs':return N(stdout=b'fake-private-log')
        if args[0]=='rm':return N(stdout=b'fake-removed')
        raise AssertionError(args)


class FirstReopenFails(FakeDocker):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.reopen_polls=0;self.retry_budget=1
    def run(self,*args,**kwargs):
        if args[0]=='run':
            result=super().run(*args,**kwargs)
            config=gate.parse((self.directory.parent/'runtime/capture.json').read_bytes())
            self.retry_budget=config['service']['max_restarts'];return result
        if args[0]=='exec' and args[-1]==gate.PROBE_WORKER and self.killed and not self.gate.joinpath('control.json').exists():
            self.reopen_polls+=1
            if self.reopen_polls<=2:
                self.status.update(ready=False,state='opening' if self.reopen_polls==1 else 'failed',pid=43,
                    session_id='e'*32,snapshot_id=None,restart_attempt=1,framesets_observed=0,pairs_accepted=0,pairs_published=0,
                    reason='SDK_OPENING' if self.reopen_polls==1 else 'PAIR_POLL_TIMEOUT')
                self.write(self.directory/'status.json',self.status)
                if self.reopen_polls==2 and self.retry_budget==1:self.stopped=True
                return N(stdout=gate.canonical(dict(status=self.status,alive=self.reopen_polls==1,start_ticks='200',parent_pid=1)))
            self.status.update(ready=True,state='ready',pid=44,session_id='a'*32,snapshot_id='9'*64,restart_attempt=2,
                framesets_observed=5,pairs_accepted=5,pairs_published=5,reason=None)
            self.killed=False;self.write(self.directory/'status.json',self.status)
            return N(stdout=gate.canonical(dict(status=self.status,alive=True,start_ticks='300',parent_pid=1)))
        return super().run(*args,**kwargs)


class NativeGateAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None)
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        config=dict(serial='synthetic-private-device',runtime_contract={'service_sha256':'a'*64},
                    service=dict(startup_timeout_ms=1000,restart_backoff_ms=100,shutdown_timeout_ms=100))
        self.config=self.root/'base.json';self.config.write_bytes(gate.canonical(config));self.config.chmod(0o600)
        self.service=self.root/'service-binary';self.service.write_bytes(b'synthetic-binary-not-executed');self.service.chmod(0o700)
        self.args=N(root=self.root/'test',base_config=self.config,image='synthetic/image:test',service_binary=self.service,
                    manager_binary=MANAGER,gate_program=PROTOCOL,ros_domain=79,execute=True,devices_released=True,recovery_restarts=1)
    def test_existing_root_is_never_modified_on_refusal(self):
        self.args.root.mkdir(mode=0o700);sentinel=self.args.root/'report.json';sentinel.write_bytes(b'existing-owned-data')
        docker=FakeDocker(PROTOCOL)
        result=gate.Acceptance(self.args,docker=docker,clock=FakeClock()).run()
        self.assertFalse(result['ok']);self.assertEqual(sentinel.read_bytes(),b'existing-owned-data');self.assertFalse(docker.calls)
    def test_no_execute_or_device_release_means_no_docker_or_artifacts(self):
        for field in ('execute','devices_released'):
            args=copy.copy(self.args);setattr(args,field,False);docker=FakeDocker(PROTOCOL)
            result=gate.Acceptance(args,docker=docker,clock=FakeClock()).run()
            self.assertFalse(result['ok']);self.assertFalse(docker.calls);self.assertFalse(args.root.exists())
    @unittest.skipUnless(PROTOCOL.exists() and MANAGER.exists(),'build gate_protocol and manager first')
    def test_fake_native_lifecycle_uses_actual_shared_verifier_and_credentialed_release(self):
        docker=FakeDocker(PROTOCOL);clock=FakeClock();clock.tick=docker.tick
        acceptance=gate.Acceptance(self.args,docker=docker,clock=clock);result=acceptance.run()
        self.assertTrue(result['ok'],result);self.assertTrue(result['simulated'])
        self.assertTrue(result['shared_rust_ack_binding_checks']);self.assertTrue(result['fresh_sdk_worker_session_after_release'])
        self.assertFalse(result['full_management_ownership_context_verified']);self.assertTrue(docker.stopped)
        self.assertTrue(self.args.root.joinpath('COMPLETE').exists())
        self.assertFalse(acceptance.gate.joinpath('control.json').exists())
        text=json.dumps(result);self.assertNotIn('synthetic-private-device',text);self.assertNotIn(acceptance.token,text)
        self.assertNotIn(str(self.root),text)
    @unittest.skipUnless(PROTOCOL.exists() and MANAGER.exists(),'build gate_protocol and manager first')
    def test_three_retry_budget_preserves_failed_zero_frame_session_before_success(self):
        self.args.recovery_restarts=3
        docker=FirstReopenFails(PROTOCOL);clock=FakeClock();clock.tick=docker.tick
        result=gate.Acceptance(self.args,docker=docker,clock=clock).run()
        self.assertTrue(result['ok'],result);self.assertTrue(result['simulated'])
        self.assertEqual(result['retry_budget'],3);self.assertEqual(result['actual_restart_delta'],2)
        self.assertEqual(result['failed_reopen_attempts'],1);self.assertEqual(result['failed_reopen_attempts_observed'],1)
        self.assertAlmostEqual(result['recovery_deadline_seconds'],13.9)
        observations=[gate.parse(line) for line in self.args.root.joinpath('recovery-observations.jsonl').read_bytes().splitlines()]
        self.assertTrue(any(v['worker'].get('status',{}).get('reason')=='PAIR_POLL_TIMEOUT' and v['worker']['status']['framesets_observed']==0 for v in observations))
        private=gate.parse(self.args.root.joinpath('report.json').read_bytes())
        self.assertIn('recovery_observations',private['private_evidence']);self.assertNotIn('private_evidence',result)
        self.assertNotIn('e'*32,json.dumps(result))

    @unittest.skipUnless(PROTOCOL.exists() and MANAGER.exists(),'build gate_protocol and manager first')
    def test_default_one_retry_negative_result_is_not_reclassified_as_success(self):
        docker=FirstReopenFails(PROTOCOL);clock=FakeClock();clock.tick=docker.tick
        result=gate.Acceptance(self.args,docker=docker,clock=clock).run()
        self.assertFalse(result['ok']);self.assertEqual(result['retry_budget'],1)
        self.assertEqual(result['actual_restart_delta'],1);self.assertEqual(result['failed_reopen_attempts'],1)
        self.assertTrue(result['credentialed_release_verified']);self.assertTrue(result['worker_reopen_blocked'])
        self.assertFalse(self.args.root.joinpath('COMPLETE').exists())
        self.assertTrue(self.args.root.joinpath('recovery-observations.jsonl').exists())

    @unittest.skipUnless(PROTOCOL.exists() and MANAGER.exists(),'build gate_protocol and manager first')
    def test_created_but_not_started_container_is_removed_after_daemon_failure(self):
        class CreatedOnly(FakeDocker):
            def run(self,*args,**kwargs):
                value=super().run(*args,**kwargs)
                if args[0]=='run':
                    self.stopped=True
                    raise gate.Failure('DOCKER_OPERATION_FAILED')
                return value
        docker=CreatedOnly(PROTOCOL)
        result=gate.Acceptance(self.args,docker=docker,clock=FakeClock()).run()
        self.assertFalse(result['ok']);self.assertTrue(result['cleanup_ok'])
        self.assertTrue(any(args[0]=='rm' for args in docker.calls))
        self.assertFalse(any(args[0]=='stop' for args in docker.calls))
        self.assertFalse(self.args.root.joinpath('COMPLETE').exists())

    @unittest.skipUnless(PROTOCOL.exists() and MANAGER.exists(),'build gate_protocol and manager first')
    def test_old_snapshot_binding_bug_is_detected_and_never_completes(self):
        docker=FakeDocker(PROTOCOL,ack_wrong_snapshot=True);clock=FakeClock();clock.tick=docker.tick
        result=gate.Acceptance(self.args,docker=docker,clock=clock).run()
        self.assertFalse(result['ok']);self.assertEqual(result['code'],'WRONG_BINDING_WAS_ACKNOWLEDGED')
        self.assertTrue(docker.stopped);self.assertFalse(self.args.root.joinpath('COMPLETE').exists())


if __name__=='__main__':unittest.main()
