#!/usr/bin/env python3
"""Inject one stopped worker into an explicitly selected isolated test service.

Requires matching immutable config and synthetic test generation, and --execute.
Never use with a recording/business deployment. No USB/sysfs writes. Captures
native-watchdog recovery using the actual SDK and actual ROS health path.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


class Failure(ValueError):
    pass


def require(ok, code):
    if not ok: raise Failure(code)


def inspect_container(name):
    v=json.loads(subprocess.check_output(['docker','inspect',name],text=True,timeout=10))[0]
    require(v['State']['Running'] and v['Config']['Labels'].get('org.geminidepthport.capture-backend')=='rust-v1', 'TEST_SERVICE_REQUIRED')
    return v


def call(name, code, request=None, timeout=15):
    out=subprocess.run(['docker','exec','-i',name,'python3','-c',code],
                       input=json.dumps(request) if request is not None else '',
                       text=True,capture_output=True,timeout=timeout)
    require(out.returncode==0, 'CONTAINER_PROBE_FAILED')
    return json.loads(out.stdout)


SNAPSHOT = """import json,time,os
from pathlib import Path
v=json.loads(Path('/tmp/capture/status.json').read_text())
v['observed_unix_ms']=time.time_ns()//1000000
try:
 os.kill(v['pid'],0);v['worker_alive']=True
 raw=Path('/proc/'+str(v['pid'])+'/stat').read_text();v['worker_start_ticks']=raw[raw.rfind(')')+2:].split()[19]
except (OSError,KeyError):v['worker_alive']=False
print(json.dumps(v))
"""
STOP_WORKER = """import json,os,sys,signal,hashlib,time
from pathlib import Path
expected=json.load(sys.stdin);raw=Path('/config/capture.json').read_bytes();config=json.loads(raw)
v=json.loads(Path(config['service']['status_path']).read_text())
if not (hashlib.sha256(raw).hexdigest()==expected['config_sha256']==v['config_sha256']
        and config['service']['generation']==expected['generation']==v['generation']
        and v['kind']=='capture-service-status' and v['ready'] is True and v['state']=='ready'
        and v['session_id']==expected['session_id'] and v['pid']==expected['pid'] and v['pid']>1
        and 0<=time.time_ns()//1000000-v['updated_unix_ms']<=2000):
    raise SystemExit(2)
stat=Path('/proc/'+str(v['pid'])+'/stat').read_text()
if stat[stat.rfind(')')+2:].split()[19]!=expected['worker_start_ticks']:raise SystemExit(2)
os.kill(v['pid'],signal.SIGSTOP)
print(json.dumps({'stopped':True}))
"""


def health(name, path):
    out=subprocess.run(['docker','exec',name,'/app/entrypoint.sh','python3','/app/health.py'],
                       text=True,capture_output=True,timeout=30)
    path.write_text(out.stdout+out.stderr)
    return out.returncode==0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--container',required=True)
    p.add_argument('--capture-config',type=Path,required=True)
    p.add_argument('--summary',type=Path,required=True)
    p.add_argument('--execute',action='store_true')
    a=p.parse_args();os.umask(0o077)
    report={'version':1,'kind':'real-sdk-worker-watchdog-recovery','ok':False,'physical_usb_disconnect_tested':False}
    before=None;injected=False;recovered=False
    try:
        require(a.execute and a.container.startswith('gdp-service-'), 'ISOLATED_TEST_EXECUTION_REQUIRED')
        config=json.loads(a.capture_config.read_bytes())
        require(config['service']['generation']=='controlled-service-validation', 'SYNTHETIC_TEST_GENERATION_REQUIRED')
        digest=hashlib.sha256(a.capture_config.read_bytes()).hexdigest()
        inspect_container(a.container)
        require(health(a.container,a.summary.with_suffix('.before-private.log')), 'INITIAL_ROS_HEALTH_FAILED')
        before=call(a.container,SNAPSHOT)
        require(before['config_sha256']==digest and before['generation']==config['service']['generation']
                and before['ready'] is True, 'INITIAL_CONFIG_OR_STATE_MISMATCH')
        started=time.monotonic()
        injected=True  # The signal may succeed even if exec output is lost.
        call(a.container,STOP_WORKER,{k:before[k] for k in ('config_sha256','generation','session_id','pid','worker_start_ticks')})
        unhealthy=not health(a.container,a.summary.with_suffix('.during-private.log'))
        require(unhealthy, 'HUNG_WORKER_WRONGLY_HEALTHY')
        deadline=started+60
        while time.monotonic()<deadline:
            try:
                after=call(a.container,SNAPSHOT)
                if (after.get('state')=='ready' and after.get('ready') is True
                    and after.get('session_id')!=before['session_id']
                    and after.get('worker_alive') and after.get('config_sha256')==digest):
                    recovered=True;break
            except (Failure,ValueError,subprocess.SubprocessError):pass
            time.sleep(.2)
        require(recovered, 'REAL_SDK_RECOVERY_TIMEOUT')
        restart_delta=after['restart_attempt']-before['restart_attempt']
        require(1 <= restart_delta <= config['service']['max_restarts']-before['restart_attempt']
                and after['generation']==before['generation'] and after['pid']!=before['pid'], 'RECOVERY_IDENTITY_INVALID')
        # Dead DDS participants may remain in discovery until their lease expires.
        # Keep readiness false until the real unique-publisher probe succeeds.
        attempts=0
        post_health=False
        while time.monotonic()<deadline:
            attempts+=1
            if health(a.container,a.summary.with_suffix(f'.after-{attempts}-private.log')):
                post_health=True
                break
            time.sleep(.2)
        require(post_health and time.monotonic()-started<=60, 'RECOVERED_ROS_HEALTH_FAILED')
        # The old PID must be reaped, not just abandoned while a new device opens.
        old=call(a.container,"import os,json,sys\npid=json.load(sys.stdin)['pid']\ntry:os.kill(pid,0);alive=True\nexcept ProcessLookupError:alive=False\nprint(json.dumps({'alive':alive}))",{'pid':before['pid']})
        require(not old['alive'], 'OLD_WORKER_NOT_REAPED')
        report.update(ok=True,initial_ros_health=True,hung_worker_reported_unhealthy=unhealthy,
                      native_watchdog_reopened_actual_sdk=True,new_session=True,old_worker_reaped=True,
                      recovered_ros_health=True,post_health_attempts=attempts,configuration_unchanged=True,
                      elapsed_until_post_health_seconds=time.monotonic()-started,
                      restart_delta=after['restart_attempt']-before['restart_attempt'])
    except Exception as exc:
        report['errors']=[str(exc) if isinstance(exc,Failure) else 'RECOVERY_ACCEPTANCE_FAILED']
    finally:
        # If the watchdog test itself aborts early, do not leave our injected STOP.
        if injected and not recovered and before:
            try:
                call(a.container,"import os,signal,json,sys\nfrom pathlib import Path\ne=json.load(sys.stdin);v=json.loads(Path('/tmp/capture/status.json').read_text())\nif v.get('pid')==e['pid'] and v.get('session_id')==e['session_id']:\n try:\n  stat=Path('/proc/'+str(e['pid'])+'/stat').read_text()\n  if stat[stat.rfind(')')+2:].split()[19]==e['worker_start_ticks']:os.kill(e['pid'],signal.SIGCONT)\n except (ProcessLookupError,FileNotFoundError):pass\nprint('{}')",{'pid':before['pid'],'session_id':before['session_id'],'worker_start_ticks':before['worker_start_ticks']})
            except Exception:report['cleanup_resume_failed']=True
        a.summary.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report));return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
