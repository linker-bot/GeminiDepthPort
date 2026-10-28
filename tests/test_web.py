import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import web_server as web

class WebFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.data = json.loads((BASE/'tests/fixtures/inventory.v1.json').read_text())
        (self.root/'inventory.json').write_text(json.dumps(self.data))
        self.patches = [patch.object(web, 'ROOT', self.root), patch.object(web.ctl,'ROOT',self.root),
                        patch.object(web.ctl,'INV',self.root/'inventory.json')]
        for p in self.patches: p.start()
        self.manager = web.Manager()
        self.devices = [{'serial_number':'HEAD_SN','product':'Orbbec Gemini 335L','model':'Gemini 335L'},
                        {'serial_number':'WRIST_SN','product':'Orbbec Gemini 2','model':'Gemini 2'}]
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()
    def payload(self):
        return {'revision':web.ctl.digest(web.ctl.INV),'cameras':[
            {'name':'head','serial_number':'HEAD_SN','parameters':{'color_fps':15}},
            {'name':'wrist','serial_number':'WRIST_SN','parameters':{'depth_height':400}}]}
    def save(self,payload):
        with patch.object(self.manager,'devices',return_value=self.devices), patch.object(self.manager,'managed',return_value=None):
            return self.manager.save_config(payload)

class WebManagerTests(WebFixture, unittest.TestCase):
    def test_model_identification_excludes_similar_models(self):
        self.assertEqual(web.identify('ORBBEC Gemini 335L'),'Gemini 335L')
        self.assertEqual(web.identify('Gemini 2'),'Gemini 2')
        for name in ('Gemini 2 L','Gemini 335Lg','Gemini 335Le','Unknown USB Camera'):
            self.assertIsNone(web.identify(name))
    def test_bind_and_save_actual_detected_models(self):
        self.save(self.payload()); data=web.ctl.load()
        self.assertEqual([c['serial_number'] for c in data['cameras']],['HEAD_SN','WRIST_SN'])
        self.assertTrue(all(not c['profiles_verified'] for c in data['cameras']))
    def test_reversed_model_binding_rejected_without_writing(self):
        before=web.ctl.INV.read_bytes(); p=self.payload();p['cameras'][0]['serial_number']='WRIST_SN'
        with self.assertRaises(ValueError): self.save(p)
        self.assertEqual(before,web.ctl.INV.read_bytes())
    def test_stale_revision_rejected(self):
        p=self.payload();p['revision']='old'
        with self.assertRaises(ValueError): self.save(p)
    def test_cannot_edit_command_or_identity_fields(self):
        p=self.payload();p['cameras'][0]['parameters']['config_file_path']='/tmp/other'
        with self.assertRaises(ValueError): self.save(p)
    def test_invalid_dimensions_rejected(self):
        p=self.payload();p['cameras'][0]['parameters']['color_width']=0
        with self.assertRaises(ValueError): self.save(p)
    def test_preview_cannot_steal_formal_service(self):
        with patch.object(self.manager,'formal_running',return_value=True):
            with self.assertRaises(ValueError): self.manager.start_preview('head')
    def test_preview_requires_right_physical_model(self):
        with patch.object(self.manager,'formal_running',return_value=False), patch.object(self.manager,'devices',return_value=[]):
            with self.assertRaises(ValueError): self.manager.start_preview('head')
    def test_no_confirmation_without_matching_live_preview(self):
        with self.assertRaises(FileNotFoundError): self.manager.confirm('head')
    def test_unknown_job_cannot_run_commands(self):
        with self.assertRaises(ValueError): self.manager.start_job('shell')
    def test_job_lock_prevents_overlapping_mutations(self):
        self.manager.lock.acquire()
        try:
            with self.assertRaises(ValueError): self.manager.start_job('up')
        finally: self.manager.lock.release()

    def test_job_results_persist_across_web_restart(self):
        job={'id':'test-job','action':'check','status':'passed','log':['healthy']}
        self.manager.persist_job(job)
        restarted=web.Manager()
        self.assertEqual(restarted.job['status'],'passed')
        self.assertTrue((self.root/'state/jobs/test-job.json').is_file())
    def test_interrupted_job_is_not_reported_as_running_after_restart(self):
        self.manager.persist_job({'id':'interrupted','action':'up','status':'running','log':[]})
        self.assertEqual(web.Manager().job['status'],'failed')

    def test_control_lock_reentrant_in_thread_but_excludes_other_threads(self):
        errors=[]
        def other():
            try:
                with web.ctl.control_lock():pass
            except web.ctl.ConfigurationError as exc:errors.append(exc.code)
        with web.ctl.control_lock():
            with web.ctl.control_lock():pass
            thread=threading.Thread(target=other);thread.start();thread.join()
        self.assertEqual(errors,['CONTROL_BUSY'])

class HttpGuardTests(unittest.TestCase):
    def handler(self,headers):
        handler=object.__new__(web.Handler)
        handler.server=SimpleNamespace(server_port=8765,manager=SimpleNamespace(token='secret'))
        handler.headers=headers
        return handler
    def test_dns_rebinding_and_cross_origin_rejected(self):
        self.assertFalse(self.handler({'Host':'evil.test:8765'}).allowed())
        self.assertFalse(self.handler({'Host':'localhost:8765','Origin':'http://evil.test'}).allowed())
        self.assertTrue(self.handler({'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765'}).allowed())
    def test_missing_csrf_rejected_before_processing_body(self):
        handler=self.handler({'Host':'127.0.0.1:8765'})
        with patch.object(handler,'send') as send:
            handler.do_POST();self.assertEqual(send.call_args.args[0],403)
    def test_unknown_static_paths_cannot_read_workspace(self):
        handler=self.handler({'Host':'127.0.0.1:8765'});handler.path='/../inventory.json'
        with patch.object(handler,'send') as send:
            handler.do_GET();self.assertEqual(send.call_args.args[0],404)


class PreviewFlowTests(WebFixture, unittest.TestCase):
    # These tests exercise container and verification plumbing without real Docker.
    def test_start_preview_builds_model_specific_mounts_and_lease(self):
        self.save(self.payload())
        with patch.object(self.manager,'formal_running',return_value=False), \
             patch.object(self.manager,'devices',return_value=self.devices), \
             patch.object(self.manager,'stop_preview'), patch.object(web,'execute',return_value='container-id') as command:
            self.manager.start_preview('wrist')
            argv=command.call_args.args[0]
            self.assertIn('CAMERA_MODEL=Gemini 2',argv)
            self.assertIn('/app/preview_session.py',argv)
            folder=self.manager.preview_root/'wrist/config'
            params=__import__('yaml').safe_load((folder/'camera.yaml').read_text())
            self.assertEqual(params['serial_number'],'WRIST_SN')
            self.assertEqual(params['depth_height'],400)
            self.assertTrue((folder/'lease').exists())
    def test_fresh_preview_still_requires_real_health_probe(self):
        self.save(self.payload())
        folder=self.manager.preview_root/'head';(folder/'config').mkdir(parents=True);(folder/'out').mkdir()
        (folder/'config/revision').write_text(self.manager.preview_signature(web.ctl.load()['cameras'][0]))
        now=__import__('time').time()
        status={s:{'received_at':now,'frames':10,'width':w,'height':h} for s,w,h in [('color',1280,720),('depth',640,480)]}
        (folder/'out/status.json').write_text(json.dumps(status))
        with patch.object(self.manager,'managed',return_value={'State':{'Running':True}}), \
             patch.object(web,'execute',side_effect=ValueError('insufficient fps')):
            with self.assertRaises(ValueError): self.manager.confirm('head')
        self.assertFalse(web.ctl.load()['cameras'][0]['profiles_verified'])
    def test_confirmation_after_successful_health_probe(self):
        self.save(self.payload())
        folder=self.manager.preview_root/'head';(folder/'config').mkdir(parents=True);(folder/'out').mkdir()
        (folder/'config/revision').write_text(self.manager.preview_signature(web.ctl.load()['cameras'][0]))
        now=__import__('time').time()
        (folder/'out/status.json').write_text(json.dumps({s:{'received_at':now,'frames':10,'width':w,'height':h} for s,w,h in [('color',1280,720),('depth',640,480)]}))
        with patch.object(self.manager,'managed',return_value={'State':{'Running':True}}), \
             patch.object(web,'execute',return_value='health passed') as command:
            self.manager.confirm('head')
            self.assertIn('/app/health.py',command.call_args.args[0])
        self.assertTrue(web.ctl.load()['cameras'][0]['profiles_verified'])

if __name__=='__main__': unittest.main()
