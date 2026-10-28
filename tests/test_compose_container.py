import legacy_support  # Enable the test-only Python behavior oracle.
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import cameractl as ctl
import deployment_control as deployment
import web_server as web

class ComposeContainerTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((BASE/'tests/fixtures/inventory.v1.json').read_text())
        for index,camera in enumerate(self.data['cameras']):
            camera['serial_number']=f'SYNTHETIC_{index}';camera['profiles_verified']=True
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(ctl,'ROOT',self.root),patch.object(ctl,'INV',self.root/'inventory.json')]
        for p in self.patches:p.start()
        ctl.INV.write_text(json.dumps(self.data))
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def test_management_mounts_preserve_host_paths_and_owner(self):
        service=ctl.web_service()
        root_mount=service['volumes'][0]
        self.assertEqual(root_mount['source'],root_mount['target'])
        self.assertEqual(service['user'],'${DEPLOY_UID:-0}:${DEPLOY_GID:-0}')
        self.assertEqual(service['group_add'],['${DOCKER_GID:-0}'])
        self.assertEqual(service['cap_drop'],['ALL']);self.assertNotIn('cap_add',service)
        self.assertEqual(service['ports'],['127.0.0.1:${WEB_PORT:-8765}:8765'])
    def test_empty_camera_group_stop_never_touches_management(self):
        with patch.object(ctl.lifecycle,'instances',return_value=[]) as discover, patch.object(ctl,'compose') as compose:
            self.assertEqual(ctl.stop_cameras()['status'],'stopped')
            compose.assert_not_called()
            self.assertTrue(all(call.args[1:] == ('camera',) for call in discover.call_args_list))
    def test_management_container_is_not_a_running_camera(self):
        with patch.object(ctl,'run',return_value='') as run,patch.object(ctl,'assert_no_previews'):
            ctl.assert_stopped()
            self.assertIn('label=orbbec.role=camera',run.call_args.args[0])
        with patch.object(web,'execute',return_value='') as execute:
            self.assertFalse(web.Manager().formal_running())
            self.assertIn('label=orbbec.role=camera',execute.call_args.args[0])
    def test_camera_restart_policy_and_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(ctl,'ROOT',root),patch.object(ctl,'INV',root/'inventory.json'),patch.object(ctl,'assert_stopped'),patch.object(ctl,'validate_bundle'):
                ctl.INV.write_text(json.dumps(self.data));ctl.render(self.data,draft=True)
                compose=__import__('yaml').safe_load((ctl.generation.load_current(root)['folder']/'compose.yaml').read_text())
            for name in ('camera_head','camera_wrist'):
                self.assertEqual(compose['services'][name]['restart'],'no')
                self.assertEqual(compose['services'][name]['profiles'],['cameras'])
            self.assertEqual(compose['services']['web']['restart'],'unless-stopped')
            self.assertEqual(compose['services']['camera_wrist']['depends_on']['camera_head']['condition'],'service_healthy')
    def test_published_custom_port_preserves_origin_checks(self):
        handler=object.__new__(web.Handler);handler.server=SimpleNamespace(server_port=8765)
        with patch.dict(os.environ,{'ORBBEC_PUBLIC_PORT':'8876'}):
            handler.headers={'Host':'localhost:8876','Origin':'http://localhost:8876'};self.assertTrue(handler.allowed())
            handler.headers={'Host':'localhost:8876','Origin':'http://evil.test'};self.assertFalse(handler.allowed())
    def test_external_boot_result_refreshes_after_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'inventory.json').write_text(json.dumps(self.data))
            with patch.object(web,'ROOT',root),patch.object(ctl,'ROOT',root),patch.object(ctl,'INV',root/'inventory.json'),patch.object(web,'execute',return_value=''):
                manager=web.Manager();manager.job={'id':'boot-test','action':'boot','status':'running','log':[]}
                manager.persist_job({'id':'boot-test','action':'boot','status':'passed','log':['healthy']})
                with patch.object(manager,'devices',return_value=[]),patch.object(manager,'managed',return_value=None):
                    self.assertEqual(manager.state()['job']['status'],'passed')


    def test_status_poll_preserves_a_boot_that_completed_before_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'inventory.json').write_text(json.dumps(self.data))
            with patch.object(web,'ROOT',root),patch.object(ctl,'ROOT',root),patch.object(ctl,'INV',root/'inventory.json'),patch.object(web,'execute',return_value=''):
                manager=web.Manager()
                passed={'id':'race','action':'boot','status':'passed','log':['healthy']}
                manager.persist_job(passed)
                original=Path.read_text;reads=[0]
                def read(path,*args,**kwargs):
                    if path==root/'state/latest-job.json':
                        reads[0]+=1
                        if reads[0]==1:return json.dumps(dict(passed,status='running'))
                    return original(path,*args,**kwargs)
                with patch.object(Path,'read_text',read),patch.object(manager,'devices',return_value=[]),patch.object(manager,'managed',return_value=None):
                    self.assertEqual(manager.state()['job']['status'],'passed')
                self.assertEqual(json.loads((root/'state/latest-job.json').read_text())['status'],'passed')
    def test_abandoned_external_boot_is_persisted_as_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'inventory.json').write_text(json.dumps(self.data))
            with patch.object(web,'ROOT',root),patch.object(ctl,'ROOT',root),patch.object(ctl,'INV',root/'inventory.json'),patch.object(web,'execute',return_value=''):
                manager=web.Manager();manager.persist_job({'id':'abandoned','action':'boot','status':'running','log':[]})
                with patch.object(manager,'devices',return_value=[]),patch.object(manager,'managed',return_value=None):
                    self.assertEqual(manager.state()['job']['status'],'failed')
                self.assertEqual(json.loads((root/'state/jobs/abandoned.json').read_text())['status'],'failed')

if __name__=='__main__':unittest.main()
