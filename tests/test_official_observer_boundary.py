"""Private boundary/process tests without importing ROS or touching hardware."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('official_observer',BASE/'runtime/official_observer.py')
observer=importlib.util.module_from_spec(spec);spec.loader.exec_module(observer)
class ObserverBoundaryTests(unittest.TestCase):
    def test_snapshot_publication_is_private_immutable_and_same_address_cannot_be_repaired(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'snapshots'/('a'*64+'.json');value={'version':1,'synthetic':'no device'}
            observer.publish_snapshot(path,value);original=path.read_bytes()
            observer.publish_snapshot(path,value);self.assertEqual(path.read_bytes(),original)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
            self.assertEqual(path.stat().st_nlink,1)
            path.write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'SNAPSHOT_ADDRESS_CORRUPTED'):observer.publish_snapshot(path,value)
            self.assertEqual(path.read_text(),'{}')
            self.assertFalse(any(p.name.startswith('.pending-') for p in path.parent.iterdir()))
    def test_protocol_uses_versioned_lines_flushes_and_reaps_only_its_child(self):
        with tempfile.TemporaryDirectory() as temp:
            child=Path(temp)/'child.py';child.write_text("import sys,json\nfor line in sys.stdin:\n v=json.loads(line)\n assert set(v)=={'version','command'} and v['version']==1\n print(json.dumps({'version':1,'ok':True,'result':v['command']}),flush=True)\n")
            real_popen=subprocess.Popen
            with patch.object(observer.subprocess,'Popen',side_effect=lambda args,**kw:real_popen([sys.executable,str(child)],**kw)):
                protocol=observer.Protocol(Path(temp)/'private.json')
            pid=protocol.child.pid
            try:
                reply=protocol.request({'kind':'query','request':{'version':1,'query':{'kind':'snapshot'}}})
                self.assertEqual(reply['result']['kind'],'query')
            finally:protocol.close()
            self.assertIsNotNone(protocol.child.poll());self.assertNotIn(protocol,observer.CORES)
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
    def test_core_eof_does_not_leave_an_observer_process_or_open_pipe(self):
        real_popen=subprocess.Popen
        with patch.object(observer.subprocess,'Popen',side_effect=lambda args,**kw:real_popen([sys.executable,'-c','pass'],**kw)):
            protocol=observer.Protocol(Path('/synthetic/config.json'))
        try:
            with self.assertRaises((RuntimeError,BrokenPipeError)):protocol.request({'kind':'tick'})
        finally:protocol.close()
        self.assertIsNotNone(protocol.child.poll());self.assertTrue(protocol.child.stdout.closed)
    def test_partial_response_cannot_hang_the_observer_supervisor(self):
        real_popen=subprocess.Popen
        child="import sys,time;sys.stdout.write('{\"version\":1,');sys.stdout.flush();time.sleep(30)"
        with patch.object(observer.subprocess,'Popen',side_effect=lambda args,**kw:real_popen([sys.executable,'-c',child],**kw)):
            protocol=observer.Protocol(Path('/synthetic/config.json'))
        try:
            with patch.object(observer,'RESPONSE_TIMEOUT',.1):
                with self.assertRaisesRegex(RuntimeError,'OBSERVER_CORE_TIMEOUT'):protocol.request({'kind':'tick'})
        finally:protocol.close()
        self.assertIsNotNone(protocol.child.poll())
    def test_driver_interruption_precedes_finalize_and_normal_stop_has_no_fault(self):
        class Core:
            def __init__(self):self.commands=[];self.reason=None
            def request(self,command):
                self.commands.append(command)
                if command['kind']=='interrupt':self.reason=command['reason']
                return {'ok':True}
            def query(self,query):
                summary={'version':1,'session_id':'a'*32,'finalized':True,'interruption_reason':self.reason,'frame_counts':{'color':0,'depth':0}}
                if query['kind']=='snapshot':return {'quality':{'result':{'snapshot':{'state':'failed' if self.reason else 'normal','current_session_finalized':True,'interruption_reason':self.reason,'finalized_sessions':[summary]}}}}
                return {'quality':{'result':{'page':{'events':[{'violation':'OBSERVER_INTERRUPTED','interruption_reason':self.reason}] if self.reason else []}}}}
        for reason in ('driver-exited','observer-interrupted',None):
            with tempfile.TemporaryDirectory() as temp:
                directory=Path(temp);core=Core()
                result=observer.finalize_observer_session(core,directory,instance='b'*64,session='a'*32,snapshot_id='c'*64,generation_id='d'*32,config_sha256='e'*64,at=lambda:{'session_id':'a'*32,'offset_ns':1},reason=reason)
                commands=[command['kind'] for command in core.commands]
                self.assertEqual(commands,['interrupt','finalize'] if reason else ['finalize'])
                current=json.loads((directory/'current.json').read_text())
                self.assertEqual(current['quality']['interruption_reason'],reason)
                self.assertEqual((directory/'interruption.json').exists(),reason is not None)
                self.assertEqual(json.loads((directory/'summary.json').read_text())['summary']['interruption_reason'],reason)
                if reason:self.assertTrue(result['core_reported']);self.assertTrue(result['quality_finalized'])
                else:self.assertIsNone(result)
    def test_dead_core_preserves_failure_without_fake_quality_or_summary(self):
        class Dead:
            def request(self,command):raise BrokenPipeError('synthetic')
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            result=observer.finalize_observer_session(Dead(),directory,instance='b'*64,session='a'*32,snapshot_id=None,generation_id='d'*32,config_sha256='e'*64,at=lambda:{'session_id':'a'*32,'offset_ns':1},reason='core-unavailable')
            self.assertFalse(result['core_reported']);self.assertFalse(result['quality_finalized'])
            self.assertFalse((directory/'summary.json').exists());self.assertFalse((directory/'events.json').exists())
            current=json.loads((directory/'current.json').read_text());self.assertIsNone(current['quality']);self.assertEqual(current['lifecycle_interruption']['reason'],'core-unavailable')
            self.assertEqual(json.loads((directory/'interruption.json').read_text())['generation_id'],'d'*32)
    def test_driver_core_error_and_normal_stop_classification_and_pre_core_failure_are_durable(self):
        from types import SimpleNamespace as NS
        driver=NS(pid=123,poll=lambda:1);core=NS(child=NS(poll=lambda:None))
        with patch.object(observer,'STOP',False):self.assertEqual(observer.session_interruption_reason(driver,core),'driver-exited')
        driver=NS(pid=123,poll=lambda:None)
        with patch.object(observer,'STOP',False):self.assertEqual(observer.session_interruption_reason(driver,core,RuntimeError('OBSERVER_CORE_TIMEOUT')),'core-unavailable')
        with patch.object(observer,'STOP',True):self.assertIsNone(observer.session_interruption_reason(driver,core))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'capture';root.mkdir(mode=0o700)
            policy=Path(temp)/'policy.json';policy.write_text(json.dumps({'generation_id':'d'*32}))
            real_path=Path
            with patch.object(observer,'ROOT',root),patch.object(observer,'STOP',False),patch.object(observer,'CORES',[]),patch.object(observer,'Path',side_effect=lambda value:policy if value=='/config/observer-policy.json' else real_path(value)),patch.object(observer,'_observe_session',side_effect=RuntimeError('synthetic initialization failure')):
                with self.assertRaises(RuntimeError):observer.observe('b'*64,driver)
            sessions=list((root/'sessions').iterdir());self.assertEqual(len(sessions),1)
            interruption=json.loads((sessions[0]/'interruption.json').read_text());self.assertEqual(interruption['reason'],'observer-interrupted');self.assertFalse(interruption['core_reported'])
            status=json.loads((root/'status.json').read_text());self.assertFalse(status['ready']);self.assertIsNone(status['snapshot_id']);self.assertFalse(status['native_session_verified'])

    def test_timeout_cannot_reuse_a_late_reply_as_finalize_ack(self):
        real_popen=subprocess.Popen
        child="import sys,json,time;sys.stdin.readline();time.sleep(.3);print(json.dumps({'version':1,'ok':True,'kind':'update'}),flush=True);sys.stdin.readline()"
        with patch.object(observer.subprocess,'Popen',side_effect=lambda args,**kw:real_popen([sys.executable,'-c',child],**kw)):
            protocol=observer.Protocol(Path('/synthetic/config.json'))
        try:
            with patch.object(observer,'RESPONSE_TIMEOUT',.05):
                with self.assertRaisesRegex(RuntimeError,'OBSERVER_CORE_TIMEOUT'):protocol.request({'kind':'tick'})
            with self.assertRaisesRegex(RuntimeError,'OBSERVER_CORE_UNAVAILABLE'):protocol.request({'kind':'finalize'})
            self.assertFalse(protocol.usable)
        finally:protocol.close()

if __name__=='__main__':unittest.main()
