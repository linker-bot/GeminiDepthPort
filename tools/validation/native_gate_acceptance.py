#!/usr/bin/env python3
"""Explicit single-device native gate fault test; never runs on import/preflight.

Uses constructed isolated Controller state for credentialed release, not a claim
of complete management ownership/context acceptance. All artifacts stay private.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import stat
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.testing.multicamera_runner import private_read,parse,canonical,exclusive,open_no_follow

LABEL='org.geminidepthport.native-gate-test'
STATUS='/tmp/capture/status.json'
CONFIG='/acceptance/capture.json'


class Failure(ValueError):pass

def require(ok,code):
    if not ok:raise Failure(code)


def sha(raw):return hashlib.sha256(raw).hexdigest()


def atomic(path,raw):
    temporary=path.with_name(path.name+'.new-'+secrets.token_hex(8))
    exclusive(temporary,raw)
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def artifact(path,destination=None):
    fd=open_no_follow(path,os.O_RDONLY|os.O_NONBLOCK)
    digest=hashlib.sha256()
    with os.fdopen(fd,'rb') as source:
        before=os.fstat(source.fileno())
        require(stat.S_ISREG(before.st_mode) and 0<before.st_size<=256*1024*1024,'BINARY_REGULAR_LIMIT')
        sink=None
        try:
            if destination is not None:
                out=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o700)
                sink=os.fdopen(out,'wb')
            while True:
                raw=source.read(1024*1024)
                if not raw:break
                digest.update(raw)
                if sink:sink.write(raw)
            after=os.fstat(source.fileno())
            require((before.st_size,before.st_mtime_ns,before.st_ino)==(after.st_size,after.st_mtime_ns,after.st_ino),'BINARY_CHANGED')
            if sink:sink.flush();os.fsync(sink.fileno())
        finally:
            if sink:sink.close()
    return digest.hexdigest()


PROBE_WORKER=r'''import json,os,sys
from pathlib import Path
v=json.loads(Path('/tmp/capture/status.json').read_text())
pid=v.get('pid');alive=False;start=None;parent=None
if type(pid)==int and pid>1:
 try:
  raw=Path('/proc/'+str(pid)+'/stat').read_text();fields=raw[raw.rfind(')')+2:].split()
  cmd=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(bytes([0]))
  alive=fields[0]!='Z' and b'--worker' in cmd and b'/acceptance/capture.json' in cmd
  start=fields[19];parent=int(fields[1])
 except FileNotFoundError:pass
print(json.dumps({'status':v,'alive':alive,'start_ticks':start,'parent_pid':parent}))
'''
KILL_WORKER=r'''import json,os,sys,signal,hashlib
from pathlib import Path
e=json.load(sys.stdin);v=json.loads(Path('/tmp/capture/status.json').read_text())
raw=Path('/acceptance/capture.json').read_bytes()
if not (v.get('ready') is True and v.get('state')=='ready' and v.get('pid')==e['pid'] and e['pid']>1
 and v.get('session_id')==e['session_id'] and v.get('snapshot_id')==e['snapshot_id']
 and v.get('config_sha256')==e['config_sha256']==hashlib.sha256(raw).hexdigest()):raise SystemExit(2)
stat=Path('/proc/'+str(e['pid'])+'/stat').read_text();fields=stat[stat.rfind(')')+2:].split()
cmd=Path('/proc/'+str(e['pid'])+'/cmdline').read_bytes().split(bytes([0]))
if fields[19]!=e['start_ticks'] or b'--worker' not in cmd or b'/acceptance/capture.json' not in cmd:raise SystemExit(2)
os.kill(e['pid'],signal.SIGKILL)
print('{"killed":true}')
'''
BLOCKED_WINDOW=r'''import json,sys,time
from pathlib import Path
e=json.load(sys.stdin);start=time.monotonic();samples=0;new_workers=0;session_changes=0;blocked=False;last={}
while True:
 for item in Path('/proc').iterdir():
  if not item.name.isdigit():continue
  try:
   cmd=(item/'cmdline').read_bytes().split(bytes([0]))
   if b'--worker' in cmd and b'/acceptance/capture.json' in cmd:
    raw=(item/'stat').read_text();fields=raw[raw.rfind(')')+2:].split()
    if fields[0]!='Z' and (int(item.name)!=e['pid'] or fields[19]!=e['start_ticks']):new_workers+=1
  except (FileNotFoundError,ProcessLookupError,PermissionError):pass
 try:
  last=json.loads(Path('/tmp/capture/status.json').read_text())
  if last.get('session_id')!=e['session_id']:session_changes+=1
  blocked=blocked or last.get('reason')=='RECORDING_PROTECTED_RECOVERY_BLOCKED'
 except (FileNotFoundError,json.JSONDecodeError):pass
 samples+=1
 if time.monotonic()-start>=e['seconds']:break
 time.sleep(.05)
print(json.dumps({'elapsed_seconds':time.monotonic()-start,'samples':samples,'new_worker_observations':new_workers,
 'session_change_observations':session_changes,'blocked_reason_observed':blocked,'last_status':last}))
'''


class Docker:
    def run(self,*args,input=None,timeout=60,check=True):
        result=subprocess.run(['docker',*args],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
        if check and result.returncode!=0:raise Failure('DOCKER_OPERATION_FAILED')
        return result
    def inspect(self,name):return parse(self.run('inspect',name).stdout)[0]


class Acceptance:
    def __init__(self,args,docker=None,clock=time):
        self.args=args;self.docker=docker or Docker();self.clock=clock
        self.simulated=docker is not None;self.run_id=secrets.token_hex(16)
        self.name='gdp-native-gate-'+self.run_id[:12]
        self.root=Path(args.root);self.controller=self.root/'controller'
        self.gate=self.controller/'state/recording-gates/camera_a'
        self.request=None;self.token=None;self.created=False;self.owns_root=False
        self.report=dict(version=1,kind='single-device-native-gate-functional',ok=False,simulated=self.simulated,
                         full_management_ownership_context_verified=False,
                         management_scope='credentialed-release-on-isolated-constructed-controller-state',
                         production_distribution_verified=False,ros_delivery_verified=False,physical_accuracy_verified=False,physical_usb_disconnect_tested=False)
    def owned(self):
        value=self.docker.inspect(self.name)
        require(value['Config']['Labels'].get(LABEL)==self.run_id,'CONTAINER_OWNER_MISMATCH')
        return value
    def probe(self,code,request=None,timeout=30):
        self.owned()
        result=self.docker.run('exec','-i',self.name,'python3','-c',code,
                               input=canonical(request) if request is not None else b'',timeout=timeout)
        require(len(result.stdout)<=4*1024*1024,'CONTAINER_RESPONSE_LIMIT')
        return parse(result.stdout)
    def guard(self,phase):
        record=dict(version=2,state=phase,generation=self.config['service']['generation'],
                    token_sha256=sha(self.token.encode()),requests=[dict(camera_name='camera_a',worker_pid=self.before['pid'],request=self.request)])
        atomic(self.controller/'state/recording-guard.json',canonical(record))
    def protocol(self,command,request_path,ack_path=None,worker=None,expect_ok=True):
        require(artifact(self.args.gate_program)==self.gate_program_sha,'GATE_PROGRAM_CHANGED')
        args=[str(self.args.gate_program),command,str(request_path)]
        if ack_path is not None:args.extend([str(ack_path),str(worker)])
        result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10)
        require(result.returncode==0 if expect_ok else result.returncode!=0,'SHARED_RUST_ACK_VERDICT_MISMATCH')
        response=parse(result.stdout)
        require(response.get('ok') is expect_ok,'SHARED_RUST_ACK_RESPONSE_MISMATCH')
        return response
    def wait_ack(self,phase):
        path=self.root/(phase+'-request.json');atomic(path,canonical(self.request))
        expected=self.protocol('request-digest',path)['request_sha256']
        deadline=self.clock.monotonic()+5
        while self.clock.monotonic()<deadline:
            try:
                ack=parse(private_read(self.gate/'ack.json'))
                if ack.get('request_sha256')==expected:
                    saved=self.root/(phase+'-ack.json');atomic(saved,canonical(ack))
                    self.protocol('verify-ack',path,saved,self.before['pid'])
                    require(ack.get('supervisor_pid')==self.worker_parent,'ACK_SUPERVISOR_MISMATCH')
                    return saved
            except FileNotFoundError:pass
            self.clock.sleep(.05)
        raise Failure('NATIVE_ACK_TIMEOUT')
    def reject_request(self,field):
        invalid=copy.deepcopy(self.request);invalid['request_id']=secrets.token_hex(16)
        if field=='session':invalid['binding']['session_id']=secrets.token_hex(16)
        else:invalid['snapshot_id']=secrets.token_hex(32)
        self.gate.joinpath('ack.json').unlink(missing_ok=True)
        atomic(self.gate/'control.json',canonical(invalid))
        atomic(self.root/('rejected-'+field+'-request.json'),canonical(invalid))
        deadline=self.clock.monotonic()+1.
        while self.clock.monotonic()<deadline:
            require(not self.gate.joinpath('ack.json').exists(),'WRONG_BINDING_WAS_ACKNOWLEDGED')
            self.clock.sleep(.05)
    def release(self,credential,expect_ok):
        require(artifact(self.args.manager_binary)==self.manager_sha,'MANAGER_BINARY_CHANGED')
        result=subprocess.run([str(self.args.manager_binary),'--root',str(self.controller),'release-protection'],
                              input=credential.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        require(result.returncode==0 if expect_ok else result.returncode!=0,'CREDENTIAL_RELEASE_VERDICT_MISMATCH')
        if not expect_ok:require(b'GUARD_CREDENTIAL' in result.stderr,'WRONG_RELEASE_FAILURE_REASON')
    def ready(self,config_sha,old_session=None):
        recovering=old_session is not None
        budget=self.args.recovery_restarts
        before_attempt=self.before.get('restart_attempt',0) if recovering else 0
        remaining=budget-before_attempt if recovering else 1
        require(remaining>0,'RECOVERY_RESTART_BUDGET_ALREADY_EXHAUSTED')
        startup=self.config['service']['startup_timeout_ms']/1000.
        if recovering:
            shutdown=self.config['service']['shutdown_timeout_ms']/1000.
            backoff=self.config['service']['restart_backoff_ms']/1000.
            # Include every remaining worker's original startup/teardown budget
            # and the supervisor's increasing configured backoff, plus the
            # existing ten-second observation grace. No service threshold changes.
            window=remaining*(startup+shutdown)+sum(backoff*min(attempt,10)
                for attempt in range(before_attempt+1,budget+1))+10.
            self.report.update(recovery_retry_budget_available=remaining,recovery_deadline_seconds=window)
        else:window=startup+10.
        start=self.clock.monotonic();deadline=start+window
        failed_sessions=set();seen_sessions=set();previous_session=None;previous_ready=False
        trace=None;trace_bytes=0
        if recovering:
            fd=os.open(self.root/'recovery-observations.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            trace=os.fdopen(fd,'wb')
        try:
            while self.clock.monotonic()<deadline:
                running=self.owned()['State']['Running']
                try:
                    info=self.probe(PROBE_WORKER) if running else dict(status=parse(private_read(self.root/'status/status.json')),alive=False,
                        source='host-status-after-container-exit')
                    v=info['status']
                except (FileNotFoundError,Failure):
                    info=dict(observation_error='WORKER_PROBE_UNAVAILABLE',container_running=running);v={}
                if trace:
                    line=canonical(dict(elapsed_seconds=self.clock.monotonic()-start,container_running=running,worker=info))+b'\n'
                    require(trace_bytes+len(line)<=64*1024*1024,'RECOVERY_OBSERVATION_OUTPUT_LIMIT')
                    trace.write(line);trace.flush();trace_bytes+=len(line)
                session=v.get('session_id')
                attempt=v.get('restart_attempt')
                if recovering and type(attempt) is int and attempt>=before_attempt:
                    delta=attempt-before_attempt
                    require(delta<=budget-before_attempt,'RECOVERY_RESTART_BUDGET_EXCEEDED')
                    self.report['actual_restart_delta']=max(self.report.get('actual_restart_delta',0),delta)
                if recovering and isinstance(session,str) and session!=old_session:
                    seen_sessions.add(session)
                    require(len(seen_sessions)<=remaining,'RECOVERY_SESSION_COUNT_EXCEEDED')
                    if previous_session and session!=previous_session and not previous_ready:failed_sessions.add(previous_session)
                    if v.get('state') in ('failed','degraded') and v.get('ready') is not True:failed_sessions.add(session)
                    previous_session=session;previous_ready=v.get('ready') is True
                    self.report['failed_reopen_attempts']=len(failed_sessions)
                    self.report['reopen_sessions_observed']=len(seen_sessions)
                require(running,'TEST_SERVICE_EXITED')
                if v.get('ready') is True and v.get('state')=='ready' and info.get('alive') and v.get('pairs_published',0)>0:
                    require(v.get('config_sha256')==config_sha and v.get('generation')==self.config['service']['generation'],'READY_IDENTITY_MISMATCH')
                    if not recovering or session!=old_session:
                        if recovering:
                            delta=v['restart_attempt']-before_attempt
                            require(1<=delta<=remaining,'FRESH_SESSION_RETRY_BUDGET_MISMATCH')
                            self.report['actual_restart_delta']=delta
                            # A successful attempt at restart d follows d-1 failed
                            # reopened workers, even if a very brief status was missed.
                            self.report['failed_reopen_attempts']=delta-1
                            self.report['failed_reopen_attempts_observed']=len(failed_sessions)
                        return info
                self.clock.sleep(.1)
            raise Failure('REAL_SDK_READY_TIMEOUT')
        finally:
            if trace:
                trace.flush();os.fsync(trace.fileno());trace.close()
    def prepare(self):
        require(type(self.args.recovery_restarts) is int and 1<=self.args.recovery_restarts<=3,'RECOVERY_RESTART_BUDGET_INVALID')
        self.report.update(retry_budget=self.args.recovery_restarts,actual_restart_delta=0,failed_reopen_attempts=0)
        require(self.simulated or os.geteuid()==0,'ROOT_EXECUTION_REQUIRED')
        require(self.args.execute and self.args.devices_released,'EXPLICIT_TEST_AND_DEVICE_RELEASE_REQUIRED')
        require(self.root.is_absolute() and not os.path.lexists(self.root),'NEW_ABSOLUTE_PRIVATE_ROOT_REQUIRED')
        require(self.args.service_binary.is_absolute() and self.args.manager_binary.is_absolute() and self.args.gate_program.is_absolute(),'ABSOLUTE_BINARIES_REQUIRED')
        config=parse(private_read(self.args.base_config))
        context=parse(self.docker.run('context','inspect').stdout)[0]
        require(context['Endpoints']['docker']['Host']=='unix:///var/run/docker.sock' and os.environ.get('DOCKER_HOST','') in ('','unix:///var/run/docker.sock'),'LOCAL_DOCKER_REQUIRED')
        security=parse(self.docker.run('info','--format','{{json .SecurityOptions}}').stdout)
        require(isinstance(security,list) and not any('rootless' in str(v) for v in security),'ROOTFUL_DOCKER_REQUIRED')
        image=self.docker.inspect(self.args.image)
        require(image['Config']['Labels'].get('org.geminidepthport.capture-backend')=='rust-v1','RUST_TEST_IMAGE_REQUIRED')
        self.image=image['Id']
        parent_fd=open_no_follow(self.root.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:
            parent_info=os.fstat(parent_fd)
            require(parent_info.st_uid==os.geteuid() and stat.S_IMODE(parent_info.st_mode)==0o700,'PRIVATE_PARENT_REQUIRED')
        finally:os.close(parent_fd)
        self.root.mkdir(mode=0o700)
        self.owns_root=True
        for directory in (self.root/'runtime',self.root/'status',self.controller/'state',self.gate):directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Intermediate management directories also stay private.
        for directory in (self.controller,self.controller/'state/recording-gates'):directory.chmod(0o700)
        service_sha=artifact(self.args.service_binary,self.root/'runtime/capture-service')
        self.manager_sha=artifact(self.args.manager_binary);self.gate_program_sha=artifact(self.args.gate_program)
        self.report['artifact_sha256']=dict(service=service_sha,manager=self.manager_sha,gate_protocol=self.gate_program_sha)
        config.pop('raw_recording',None)
        config['runtime_contract']['service_sha256']=service_sha
        config['service'].update(status_path=STATUS,recording_control_path='/recording/control.json',max_restarts=self.args.recovery_restarts,generation=secrets.token_hex(16))
        self.config=config;raw=canonical(config);self.config_sha=sha(raw)
        exclusive(self.root/'runtime/capture.json',raw)
    def run(self):
        try:
            self.prepare()
            self.created=True # daemon may create the container even if the client fails.
            self.docker.run('run','-d','--name',self.name,'--label',LABEL+'='+self.run_id,'--network','host','--ipc','private',
                '--read-only','--cap-drop','ALL','--device','/dev/bus/usb','--tmpfs','/tmp:rw,nosuid,nodev,size=256m',
                '--log-driver','local','--log-opt','max-size=4m','--log-opt','max-file=1','--log-opt','compress=false',
                '-e','ROS_DOMAIN_ID='+str(self.args.ros_domain),
                '-v',str(self.root/'runtime')+':/acceptance:ro','-v',str(self.root/'status')+':/tmp/capture',
                '-v',str(self.gate)+':/recording','--entrypoint','bash',self.image,'-lc',
                'source /opt/ros/humble/setup.bash; exec /acceptance/capture-service --config /acceptance/capture.json')
            initial=self.ready(self.config_sha);self.before=initial['status'];self.report['initial_restart_attempt']=self.before['restart_attempt'];self.worker_parent=initial['parent_pid']
            atomic(self.root/'initial-worker.json',canonical(initial))
            require(isinstance(self.before.get('snapshot_id'),str) and re.fullmatch('[0-9a-f]{64}',self.before['snapshot_id']), 'CURRENT_SNAPSHOT_REFERENCE')
            snapshot_path=self.root/'status/snapshots'/(self.before['snapshot_id']+'.json')
            snapshot=parse(private_read(snapshot_path));binding=snapshot['content']['binding']
            require(snapshot['snapshot_id']==self.before['snapshot_id'] and binding['session_id']==self.before['session_id']
                    and binding['instance_id']==self.before['instance_id'] and binding['generation_id']==self.before['generation'],'CURRENT_SNAPSHOT_BINDING')
            self.token=secrets.token_hex(32)
            self.request=dict(version=1,phase='preparing',request_id=secrets.token_hex(16),
                binding={k:binding[k] for k in ('instance_id','session_id','generation_id','configuration_id','calibration_id')},
                snapshot_id=snapshot['snapshot_id'],runtime_config_sha256=self.config_sha,token_sha256=sha(self.token.encode()))
            exclusive(self.controller/'state/recording-recovery-credential',self.token.encode())
            self.guard('preparing')
            self.reject_request('session');self.reject_request('snapshot')
            self.report['wrong_session_request_not_acknowledged']=True;self.report['wrong_snapshot_request_not_acknowledged']=True
            atomic(self.gate/'control.json',canonical(self.request));preparing=self.wait_ack('preparing')
            self.request['phase']='active';atomic(self.gate/'control.json',canonical(self.request))
            active=self.wait_ack('active');self.guard('active')
            self.protocol('verify-ack',self.root/'active-request.json',preparing,self.before['pid'],False)
            tampered=parse(private_read(active));tampered['session_id']=secrets.token_hex(16)
            atomic(self.root/'wrong-session-ack.json',canonical(tampered))
            self.protocol('verify-ack',self.root/'active-request.json',self.root/'wrong-session-ack.json',self.before['pid'],False)
            self.protocol('verify-ack',self.root/'active-request.json',active,self.before['pid']+1,False)
            self.report['native_preparing_active_ack']=True;self.report['shared_rust_ack_binding_checks']=True
            self.probe(KILL_WORKER,{**{k:self.before[k] for k in ('pid','session_id','snapshot_id','config_sha256')},'start_ticks':initial['start_ticks']})
            seconds=max(2*self.config['service']['restart_backoff_ms']/1000.,1.)
            blocked=self.probe(BLOCKED_WINDOW,{**{k:self.before[k] for k in ('pid','session_id')},'start_ticks':initial['start_ticks'],'seconds':seconds},timeout=seconds+15)
            atomic(self.root/'protected-window.json',canonical(blocked))
            require(blocked['elapsed_seconds']>=seconds and blocked['new_worker_observations']==0 and blocked['session_change_observations']==0
                    and blocked['blocked_reason_observed'],'PROTECTED_WORKER_REOPENED_OR_NOT_OBSERVED')
            require(self.owned()['State']['Running'],'SUPERVISOR_DIED_DURING_PROTECTION')
            require(not self.probe(PROBE_WORKER)['alive'],'SIGKILL_WORKER_REMAINED_ALIVE')
            self.report.update(worker_sigkill_injected=True,protected_observation_seconds=blocked['elapsed_seconds'],
                               protected_samples=blocked['samples'],worker_reopen_blocked=True,session_reopen_blocked=True)
            self.release('definitely-not-the-credential',False)
            require(self.gate.joinpath('control.json').is_file() and self.controller.joinpath('state/recording-guard.json').is_file(),'WRONG_CREDENTIAL_REMOVED_PROTECTION')
            self.release(self.token,True)
            require(not self.gate.joinpath('control.json').exists() and not self.gate.joinpath('ack.json').exists()
                    and not self.controller.joinpath('state/recording-guard.json').exists(),'CREDENTIAL_RELEASE_INCOMPLETE')
            self.report.update(credentialed_release_verified=True,wrong_credential_refused=True)
            resumed=self.ready(self.config_sha,self.before['session_id']);after=resumed['status']
            require(after['instance_id']==self.before['instance_id'] and after['pid']!=self.before['pid']
                    and after['snapshot_id']!=self.before['snapshot_id'] and 1<=after['restart_attempt']-self.before['restart_attempt']<=self.args.recovery_restarts,'FRESH_SESSION_IDENTITY_MISMATCH')
            atomic(self.root/'resumed-worker.json',canonical(resumed))
            self.report.update(credentialed_release_verified=True,wrong_credential_refused=True,fresh_sdk_worker_session_after_release=True,
                               immutable_configuration_preserved=True,restart_delta=after['restart_attempt']-self.before['restart_attempt'],ok=True)
        except Exception as exc:
            self.report['ok']=False;self.report['code']=str(exc) if isinstance(exc,Failure) else 'NATIVE_GATE_ACCEPTANCE_FAILED'
        finally:
            cleanup_ok=True
            if self.created:
                try:
                    info=self.owned()
                    if info['State']['Running']:self.docker.run('stop','-t','8',self.name)
                    if self.root.exists():exclusive(self.root/'container.log',self.docker.run('logs',self.name,check=False).stdout)
                    self.owned()
                    self.docker.run('rm',self.name)
                except Exception:cleanup_ok=False
            self.report['cleanup_ok']=cleanup_ok
            if not cleanup_ok:self.report['ok']=False
            if self.owns_root and self.root.exists():
                private_report=dict(self.report)
                private_report['private_evidence']={name:str(self.root/path) for name,path in {
                    'initial_worker':'initial-worker.json','protected_window':'protected-window.json',
                    'recovery_observations':'recovery-observations.jsonl','resumed_worker':'resumed-worker.json',
                    'container_log':'container.log','all_session_status_and_quality':'status'}.items() if (self.root/path).exists()}
                atomic(self.root/'report.json',canonical(private_report))
                if self.report['ok']:exclusive(self.root/'COMPLETE',b'native gate functional only; inspect scope\n')
        return self.report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-config',type=Path,required=True)
    parser.add_argument('--image',required=True)
    parser.add_argument('--service-binary',type=Path,required=True)
    parser.add_argument('--manager-binary',type=Path,required=True)
    parser.add_argument('--gate-program',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--ros-domain',type=int,required=True)
    parser.add_argument('--recovery-restarts',type=int,choices=(1,2,3),default=1,
                        help='Explicit isolated test recovery budget; default 1 preserves the original single-retry test')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--devices-released',action='store_true')
    args=parser.parse_args()
    if not 1<=args.ros_domain<=100:parser.error('bounded ROS domain required')
    result=Acceptance(args).run()
    # Private report contains hashes only; no session/serial/path/token is printed.
    print(json.dumps(result,allow_nan=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':sys.exit(main())
