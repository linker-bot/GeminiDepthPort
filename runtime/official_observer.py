"""Read-only official ROS observer boundary and supervised driver lifecycle.

SDK identities remain unknown. Timestamp association is explicitly observed,
never SDK frameset evidence. Native SDK auto-reopen cannot be gated here.
"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path('/tmp/capture-observer')
STOP = False
CORES = []
RESPONSE_TIMEOUT = 3

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary=path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
    try:
        with temporary.open('x') as handle:
            os.chmod(temporary,0o600);json.dump(value,handle,sort_keys=True,allow_nan=False)
            handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def publish_snapshot(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent,0o700)
    temporary=path.with_name('.pending-'+uuid.uuid4().hex)
    try:
        atomic(temporary,value)
        try:os.link(temporary,path,follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or json.loads(path.read_text())!=value:raise RuntimeError('SNAPSHOT_ADDRESS_CORRUPTED')
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if temporary.exists():temporary.unlink()


def stop(signum=None, frame=None):
    global STOP
    STOP=True


def terminate(child):
    if child is None or child.poll() is not None:return
    try:os.killpg(child.pid,signal.SIGTERM)
    except ProcessLookupError:return
    try:child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=3)


class Protocol:
    def __init__(self, config):
        self.config=Path(config)
        self.child=subprocess.Popen(['/usr/local/bin/capture-observer','--config',str(config)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,start_new_session=True)
        self.buffer=bytearray();self.usable=True
        CORES.append(self)
        self.selector=selectors.DefaultSelector();self.selector.register(self.child.stdout,selectors.EVENT_READ)
    def request(self, command):
        if not self.usable:raise RuntimeError('OBSERVER_CORE_UNAVAILABLE')
        try:return self._request(command)
        except Exception:
            # A timed-out/malformed exchange can leave an uncorrelated reply
            # pending. Never reuse it as ACK for interruption/finalization.
            self.usable=False
            raise
    def _request(self, command):
        self.child.stdin.write(json.dumps({'version':1,'command':command},allow_nan=False).encode()+b'\n');self.child.stdin.flush()
        deadline=time.monotonic()+RESPONSE_TIMEOUT
        while b'\n' not in self.buffer:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not self.selector.select(timeout=remaining):raise RuntimeError('OBSERVER_CORE_TIMEOUT')
            block=os.read(self.child.stdout.fileno(),65536)
            if not block:raise RuntimeError('OBSERVER_CORE_IO')
            self.buffer.extend(block)
            if len(self.buffer)>4*1024*1024:raise RuntimeError('OBSERVER_CORE_LIMIT')
        position=self.buffer.index(b'\n');line=bytes(self.buffer[:position]);del self.buffer[:position+1]
        try:response=json.loads(line)
        except ValueError:raise RuntimeError('OBSERVER_CORE_PROTOCOL') from None
        if not isinstance(response,dict) or response.get('version')!=1:raise RuntimeError('OBSERVER_CORE_PROTOCOL')
        if response.get('ok') is not True:raise RuntimeError('OBSERVER_CORE_REJECTED')
        return response
    def query(self, query):return self.request({'kind':'query','request':{'version':1,'query':query}})['result']
    def close(self):
        try:terminate(self.child)
        finally:
            self.selector.close()
            for stream in (self.child.stdin,self.child.stdout):
                try:stream.close()
                except OSError:pass
            if self in CORES:CORES.remove(self)


def session_interruption_reason(driver, core=None, error=None):
    if STOP:return None
    if driver.poll() is not None:return 'driver-exited'
    if core is not None and core.child.poll() is not None:return 'core-unavailable'
    if isinstance(error,BrokenPipeError) or str(error) in ('OBSERVER_CORE_TIMEOUT','OBSERVER_CORE_IO','OBSERVER_CORE_LIMIT','OBSERVER_CORE_REJECTED','OBSERVER_CORE_PROTOCOL','OBSERVER_CORE_UNAVAILABLE'):return 'core-unavailable'
    return 'observer-interrupted'


def finalize_observer_session(core, directory, *, instance, session, snapshot_id,
                              generation_id, config_sha256, at, reason=None):
    """Persist controller facts even when no usable core remains. Never invent
    a finalized QualitySnapshot/event after losing the core protocol."""
    lifecycle = None
    core_finalized = False
    def interrupted(cause):
        return {'version':1,'instance_id':instance,'session_id':session,
                'snapshot_id':snapshot_id,'generation_id':generation_id,
                'config_sha256':config_sha256,'reason':cause,'at':at(),
                'observed_unix_ns':time.time_ns(),'core_reported':False,
                'quality_finalized':core_finalized}
    if reason:
        lifecycle=interrupted(reason)
        atomic(directory/'interruption.json',lifecycle)
    try:
        if lifecycle:
            core.request({'kind':'interrupt','at':lifecycle['at'],'reason':reason})
            lifecycle['core_reported']=True
            atomic(directory/'interruption.json',lifecycle)
        core.request({'kind':'finalize'})
        core_finalized=True
        final=core.query({'kind':'snapshot'})['quality']['result']['snapshot']
        page=core.query({'kind':'events','cursor':None,'limit':1024})['quality']['result']['page']
        if lifecycle:
            lifecycle['quality_finalized']=True
            atomic(directory/'interruption.json',lifecycle)
        atomic(directory/'summary.json',{'version':1,'instance_id':instance,
            'session_id':session,'summary':final['finalized_sessions'][-1] if final['finalized_sessions'] else None,
            'lifecycle_interruption':lifecycle})
        atomic(directory/'current.json',{'version':1,'kind':'official-observer-quality',
            'instance_id':instance,'session_id':session,'snapshot_id':snapshot_id,
            'quality':final,'lifecycle_interruption':lifecycle})
        atomic(directory/'events.json',page)
    except Exception:
        if lifecycle is None:lifecycle=interrupted('core-unavailable')
        lifecycle['quality_finalized']=core_finalized
        atomic(directory/'interruption.json',lifecycle)
        path=directory/'current.json'
        current=json.loads(path.read_text()) if path.exists() else {
            'version':1,'kind':'official-observer-quality','instance_id':instance,
            'session_id':session,'snapshot_id':snapshot_id,'quality':None}
        current['lifecycle_interruption']=lifecycle
        atomic(path,current)
    return lifecycle

def observe(instance, driver):
    policy_path=Path('/config/observer-policy.json');policy=json.loads(policy_path.read_text())
    config_sha=hashlib.sha256(policy_path.read_bytes()).hexdigest()
    session=uuid.uuid4().hex;started=time.monotonic_ns();directory=ROOT/'sessions'/session
    directory.mkdir(parents=True,mode=0o700)
    try:
        return _observe_session(instance,driver,policy_path,policy,config_sha,session,started,directory)
    except Exception as error:
        if not STOP and not (directory/'interruption.json').exists():
            bound_core=next((c for c in CORES if getattr(c,'config',None)==directory/'observer.json'),None)
            reason=session_interruption_reason(driver,bound_core,error)
            snapshot_id=None
            try:snapshot_id=json.loads((directory/'observer.json').read_text())['streams']['color']['context']['snapshot_id']
            except (OSError,ValueError,KeyError,TypeError):pass
            if bound_core is not None:
                lifecycle=finalize_observer_session(bound_core,directory,instance=instance,session=session,
                    snapshot_id=snapshot_id,generation_id=policy['generation_id'],config_sha256=config_sha,
                    at=lambda:{'session_id':session,'offset_ns':time.monotonic_ns()-started},reason=reason)
            else:
                lifecycle={'version':1,'instance_id':instance,'session_id':session,'snapshot_id':snapshot_id,
                    'generation_id':policy['generation_id'],'config_sha256':config_sha,'reason':reason,
                    'at':{'session_id':session,'offset_ns':time.monotonic_ns()-started},
                    'observed_unix_ns':time.time_ns(),'core_reported':False,'quality_finalized':False}
                atomic(directory/'interruption.json',lifecycle)
                atomic(directory/'current.json',{'version':1,'kind':'official-observer-quality',
                    'instance_id':instance,'session_id':session,'snapshot_id':snapshot_id,'quality':None,
                    'lifecycle_interruption':lifecycle})
            atomic(ROOT/'status.json',{'version':1,'kind':'official-observer-status','ready':False,
                'state':'degraded','reason':'OFFICIAL_OBSERVER_SESSION_FAILED','pid':os.getpid(),
                'driver_pid':driver.pid,'instance_id':instance,'session_id':session,
                'session_kind':'observer','generation':policy['generation_id'],'config_sha256':config_sha,
                'snapshot_id':snapshot_id,'snapshot_path':str(ROOT/'snapshots'/(snapshot_id+'.json')) if snapshot_id else None,'quality_directory':str(directory),
                'native_session_verified':False,'native_restart_gate':False,
                'observer_session_finalized':False,'lifecycle_interruption':lifecycle,
                'updated_unix_ms':time.time_ns()//1000000})
        raise


def _observe_session(instance,driver,policy_path,policy,config_sha,session,started,directory):
    from calibration_export import collect
    actual=collect(policy['request'],15,manage_signals=False,stop_requested=lambda:STOP)
    atomic(directory/'observations.json',actual)
    bootstrap=subprocess.run(['/usr/local/bin/observer-bootstrap',str(policy_path),str(directory/'observations.json'),instance,session,str(time.time_ns())],text=True,capture_output=True,timeout=10)
    if bootstrap.returncode:
        (directory/'bootstrap-private.log').write_text(bootstrap.stderr);os.chmod(directory/'bootstrap-private.log',0o600)
        raise RuntimeError('OBSERVER_BOOTSTRAP_FAILED')
    artifact=json.loads(bootstrap.stdout);envelope=artifact['snapshot'];snapshot_id=envelope['snapshot_id']
    snapshot_path=ROOT/'snapshots'/(snapshot_id+'.json');publish_snapshot(snapshot_path,envelope)
    atomic(directory/'observer.json',artifact['observer_config'])
    core=Protocol(directory/'observer.json');server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    query_path=ROOT/'query.sock'
    if query_path.exists():query_path.unlink()
    server.bind(str(query_path));os.chmod(query_path,0o600);server.listen(4);server.setblocking(False)
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy,HistoryPolicy
    from sensor_msgs.msg import Image,CameraInfo
    context=Context();rclpy.init(context=context);node=rclpy.create_node('official_observer_'+session[:10],context=context)
    executor=SingleThreadedExecutor(context=context);executor.add_node(node)
    qos=QoSProfile(depth=10,history=HistoryPolicy.KEEP_LAST,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
    subscriptions=[];counts={'color':0,'depth':0};last_images={};last_persist=0.;heartbeat=0
    def at():return {'session_id':session,'offset_ns':time.monotonic_ns()-started}
    def stamp(msg):return {'sec':msg.header.stamp.sec,'nanosec':msg.header.stamp.nanosec}
    def callback(role,kind):
        def consume(msg):
            if kind=='image':
                value={'stamp':stamp(msg),'frame_id':msg.header.frame_id,'width':msg.width,'height':msg.height,'step':msg.step,'encoding':msg.encoding,'is_bigendian':msg.is_bigendian,'payload_len':len(msg.data)}
                core.request({'kind':'image','stream':role,'at':at(),'received_unix_ns':time.time_ns(),'image':value})
                counts[role]+=1;last_images[role]=time.monotonic()
            else:
                value={'stamp':stamp(msg),'frame_id':msg.header.frame_id,'width':msg.width,'height':msg.height,'distortion_model':msg.distortion_model,'k':list(msg.k),'d':list(msg.d),'r':list(msg.r),'p':list(msg.p),'binning':[msg.binning_x,msg.binning_y],'roi':[msg.roi.x_offset,msg.roi.y_offset,msg.roi.width,msg.roi.height],'roi_do_rectify':msg.roi.do_rectify}
                core.request({'kind':'camera-info','stream':role,'at':at(),'info':value})
        return consume
    for role in ('color','depth'):
        base=policy['namespace'].rstrip('/')+'/'+role
        subscriptions += [node.create_subscription(Image,base+'/image_raw',callback(role,'image'),qos),node.create_subscription(CameraInfo,base+'/camera_info',callback(role,'info'),qos)]
    failure_reason=None
    try:
        while not STOP and driver.poll() is None and core.child.poll() is None:
            executor.spin_once(timeout_sec=.02)
            try:
                connection,_=server.accept()
            except BlockingIOError:connection=None
            if connection:
                with connection:
                    connection.settimeout(2);raw=bytearray()
                    while b'\n' not in raw and len(raw)<=65536:
                        block=connection.recv(4096)
                        if not block:break
                        raw.extend(block)
                    try:
                        if len(raw)>65536:raise ValueError('QUERY_LIMIT')
                        request=json.loads(raw)
                        if set(request)!={'version','query'} or request['version']!=1:raise ValueError('QUERY_SCHEMA')
                        response={'version':1,'ok':True,'result':core.query(request['query'])}
                    except Exception:response={'version':1,'ok':False,'error':{'code':'QUALITY_QUERY_REJECTED'}}
                    connection.sendall(json.dumps(response,allow_nan=False).encode()+b'\n')
            if time.monotonic()-last_persist>=.5:
                core.request({'kind':'tick','at':at(),'received_unix_ns':time.time_ns()})
                core.request({'kind':'watermark','at':at(),'unix_ns':max(1,time.time_ns()-100_000_000)})
                current=core.query({'kind':'snapshot'});page=core.query({'kind':'events','cursor':None,'limit':1024})['quality']['result']['page']
                atomic(directory/'current.json',{'version':1,'kind':'official-observer-quality','instance_id':instance,'session_id':session,'snapshot_id':snapshot_id,'quality':current['quality']['result']['snapshot'],'association':current['association']})
                atomic(directory/'events.json',page)
                heartbeat+=1
                ready=all(counts[r]>=3 and time.monotonic()-last_images.get(r,0)<=3 for r in counts)
                atomic(ROOT/'status.json',{'version':1,'kind':'official-observer-status','ready':ready,'state':'ready' if ready else 'starting','pid':os.getpid(),'driver_pid':driver.pid,'instance_id':instance,'session_id':session,'session_kind':'observer','generation':policy['generation_id'],'config_sha256':config_sha,'snapshot_id':snapshot_id,'snapshot_path':str(snapshot_path),'quality_directory':str(directory),'heartbeat_sequence':heartbeat,'updated_unix_ms':time.time_ns()//1000000,'last_receive_age_ms':max((time.monotonic()-last_images.get(r,time.monotonic()))*1000 for r in counts),'received_counts':counts,'native_session_verified':False,'native_restart_gate':False,'pairing_provenance':'observed_timestamp','same_sdk_frameset':None,'physical_accuracy_verified':False})
                last_persist=time.monotonic()
        if not STOP:
            failure_reason=session_interruption_reason(driver,core)
            raise RuntimeError('OBSERVER_OR_DRIVER_EXITED')
    except Exception as error:
        if failure_reason is None and not STOP:failure_reason=session_interruption_reason(driver,core,error)
        raise
    finally:
        lifecycle=finalize_observer_session(core,directory,instance=instance,session=session,
            snapshot_id=snapshot_id,generation_id=policy['generation_id'],config_sha256=config_sha,
            at=at,reason=failure_reason)
        status_path=ROOT/'status.json'
        previous=json.loads(status_path.read_text()) if status_path.exists() else {}
        previous.update(version=1,kind='official-observer-status',ready=False,
            state='degraded' if lifecycle else 'stopped',reason='OFFICIAL_OBSERVER_SESSION_FAILED' if lifecycle else 'SIGNAL',
            pid=os.getpid(),driver_pid=driver.pid,instance_id=instance,session_id=session,
            session_kind='observer',native_session_verified=False,generation=policy['generation_id'],
            config_sha256=config_sha,snapshot_id=snapshot_id,snapshot_path=str(snapshot_path),
            quality_directory=str(directory),observer_session_finalized=lifecycle is None or lifecycle['quality_finalized'],
            lifecycle_interruption=lifecycle,updated_unix_ms=time.time_ns()//1000000)
        atomic(status_path,previous)
        server.close()
        if query_path.exists():query_path.unlink()
        core.close();executor.shutdown();node.destroy_node();context.shutdown()


def main():
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    instance=uuid.uuid4().hex+uuid.uuid4().hex;driver=None
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(ROOT,0o700)
    try:
        driver=subprocess.Popen([sys.executable,'/app/launch_camera.py'],start_new_session=True)
        for attempt in range(4):
            if STOP or driver.poll() is not None:break
            try:observe(instance,driver)
            except Exception:
                for core in list(CORES):core.close()
                if STOP:break
                previous=json.loads((ROOT/'status.json').read_text()) if (ROOT/'status.json').exists() else {}
                previous.update(version=1,kind='official-observer-status',ready=False,state='degraded',reason='OFFICIAL_OBSERVER_SESSION_FAILED',instance_id=instance,updated_unix_ms=time.time_ns()//1000000,native_restart_gate=False)
                atomic(ROOT/'status.json',previous)
                if not STOP:time.sleep(min(attempt+1,3))
        return 0 if STOP else 1
    finally:
        for core in list(CORES):core.close()
        terminate(driver)


if __name__=='__main__':raise SystemExit(main())
