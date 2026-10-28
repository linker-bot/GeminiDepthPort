"""Black-box Rust CLI/HTTP checks. Docker is a test executable, never a daemon."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

BASE=Path(__file__).resolve().parents[2]
BINARY=Path(os.environ.get('GDP_BINARY',BASE/'target/debug/geminidepthport')).resolve()

class RustManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.data=json.loads((BASE/'inventory.v2.example.json').read_text())
        (self.root/'inventory.json').write_text(json.dumps(self.data))
        shutil.copytree(BASE/'web',self.root/'web')
        shutil.copytree(BASE/'patches/public-orbbec',self.root/'patches/public-orbbec')
        (self.root/'bin').mkdir()
        docker=self.root/'bin/docker'
        docker.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
from pathlib import Path
root=Path(os.environ['ORBBEC_DEPLOY_DIR'])
a=sys.argv[1:]
with (root/'docker-calls.jsonl').open('a') as f:f.write(json.dumps(a)+'\\n')
if a[:2]==['ps','-aq'] and (root/'inspect-fixture.json').exists():
    for cid in json.loads((root/'inspect-fixture.json').read_text())['ids']:print(cid)
elif a and a[0]=='inspect' and (root/'inspect-fixture.json').exists():
    print(json.dumps(json.loads((root/'inspect-fixture.json').read_text())['values']))
elif a and a[0]=='build' and (root/'slow-build').exists():
    import time
    print('build-started',flush=True)
    time.sleep(30)
elif a[:2]==['ps','-aq']:
    if (root/'foreign').exists():print('a'*64)
elif a and a[0]=='inspect':
    if (root/'foreign').exists():print(json.dumps([{'Id':'a'*64,'Config':{'Labels':{}},'State':{'Running':True}}]))
    else:print('[]')
elif a and a[0]=='compose':
    if a[-2:]==['config','--quiet']:
        p=Path(a[a.index('-f')+1]);json.loads(p.read_text())
    if a[-1]=='logs':print('synthetic log')
elif a and a[0]=='logs':print('synthetic log')
elif a and a[0]=='run':print('synthetic enumeration result')
elif a and a[0] in ('exec','stop'):
    if (root/'rewrite-inventory.json').exists():
        (root/'inventory.json').write_bytes((root/'rewrite-inventory.json').read_bytes())
    if a[0]=='exec' and (root/'replace-preview').exists():
        fixture=json.loads((root/'inspect-fixture.json').read_text())
        fixture['ids']=['b'*64];fixture['values'][0]['Id']='b'*64
        (root/'inspect-fixture.json').write_text(json.dumps(fixture))
    if a[0]=='exec' and (root/'require-fresh-lease').exists():
        import time
        if time.time()-(root/'work/web-preview/camera_a/config/lease').stat().st_mtime>2:sys.exit(97)
''')
        docker.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH'])
        self.env.pop('ORBBEC_CONTAINERIZED',None)
        self.process=None
    def tearDown(self):
        if self.process:
            self.process.terminate()
            try:self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.process.stdout.close()
        self.tmp.cleanup()
    def cli(self,*args,ok=True,stdin=None):
        result=subprocess.run([str(BINARY),'--root',str(self.root),*args],input=stdin,text=True,capture_output=True,env=self.env,timeout=15)
        if ok:self.assertEqual(result.returncode,0,result.stderr)
        else:self.assertNotEqual(result.returncode,0)
        return result
    def start(self):
        self.process=subprocess.Popen([str(BINARY),'--root',str(self.root),'serve','--port','0'],env=self.env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        line=self.process.stdout.readline();match=re.search(r'http://127.0.0.1:(\d+)',line)
        self.assertIsNotNone(match,line)
        self.base='http://127.0.0.1:'+match.group(1)
        self.token=re.search(r'name="csrf-token" content="([^"]+)"',self.request('/').decode()).group(1)
    def request(self,path,body=None,headers=None):
        data=None if body is None else json.dumps(body).encode()
        h={} if body is None else {'Content-Type':'application/json','X-CSRF-Token':self.token,'Origin':self.base}
        h.update(headers or {})
        with build_opener(ProxyHandler({})).open(Request(self.base+path,data=data,headers=h),timeout=10) as response:return response.read()
    def error(self,code,path,body=None,headers=None):
        with self.assertRaises(HTTPError) as exc:self.request(path,body,headers)
        with exc.exception as response:
            self.assertEqual(response.code,code)
            return json.loads(response.read())
    def test_unchanged_save_without_docker_keeps_change_guard(self):
        docker=self.root/'bin/docker'
        docker.write_text('#!/bin/sh\nexit 1\n')
        self.start()
        state=json.loads(self.request('/api/state'))
        self.assertTrue(state['docker_error'])
        payload={'revision':state['revision'],'cameras':[{'name':c['name'],'parameters':{}} for c in self.data['cameras']]}
        self.assertIn('message',json.loads(self.request('/api/config',payload)))
        payload['revision']=json.loads(self.request('/api/state'))['revision']
        payload['cameras'][0]['parameters']['color_width']=1280
        before=(self.root/'inventory.json').read_bytes()
        self.error(400,'/api/config',payload)
        self.assertEqual((self.root/'inventory.json').read_bytes(),before)
    def test_http_and_csrf_revision_and_shared_cli_lock(self):
        self.start();self.assertEqual(json.loads(self.request('/healthz')),{'status':'ok'})
        state=json.loads(self.request('/api/state'));self.assertEqual(len(state['inventory']['cameras']),3)
        payload={'revision':state['revision'],'cameras':[{'name':c['name'],'parameters':{}} for c in self.data['cameras']]}
        self.assertIn('message',json.loads(self.request('/api/config',payload)))
        self.assertIn('INVENTORY_DRIFT',self.error(400,'/api/config',payload)['error'])
        self.error(403,'/api/state',headers={'Host':'evil.example'})
        self.error(403,'/api/state',headers={'Origin':'https://evil.example'})
        self.error(403,'/api/config',payload,{'X-CSRF-Token':'wrong'})
        self.error(400,'/api/job',{'action':'shell'})
        self.error(400,'/api/config',payload,{'Content-Type':'text/plain'})
        self.error(400,'/api/config',{'oversize':'x'*40000})
        payload['revision']=json.loads(self.request('/api/state'))['revision']
        with (self.root/'state/control.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIn('CONTROL_BUSY',self.error(400,'/api/config',payload)['error'])
        (self.root/'state/recording-guard.json').write_text('corrupt')
        self.assertIn('RECORDING_PROTECTED',self.error(400,'/api/config',payload)['error'])
        self.cli('release-protection',ok=False,stdin='wrong')
        # Read-only health/state stay available during recording protection.
        self.request('/api/state');self.cli('validate','--draft')
    def test_rust_confirm_rejects_shared_policy_drift(self):
        original=copy.deepcopy(self.data)
        original['capture_backend']='rust'
        original['rust_capture']={'depth_unit_policy':'normalize-mm','pairing':{'clock':'device','max_delta_us':20000},'poll_ms':100,'capacity':4,'startup_discovery_seconds':2,'service':{'idle_timeout_ms':2000,'startup_timeout_ms':15000,'heartbeat_ms':500,'heartbeat_timeout_ms':10000,'max_restarts':3,'restart_backoff_ms':1000,'shutdown_timeout_ms':5000,'ready_min_pairs':5}}
        camera=original['cameras'][0]
        camera.update(enabled=True,serial_number='SYNTHETIC_CAPTURE_ONLY',timestamp_source='host')
        camera['health']={'discovery_seconds':1,'sample_seconds':2,'freshness_seconds':2,'min_fps_ratio':0.8}
        for stream in ('color','depth'):
            for key,value in (('width',640),('height',480),('fps',30)):
                camera['parameters'][stream+'_'+key]=value
        raw={k:v for k,v in camera.items() if k not in ('verification','profiles_verified')}
        legacy=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()
        shared={k:v for k,v in original.items() if k!='cameras'}
        revision=hashlib.sha256(json.dumps({'shared':shared,'camera':raw},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        folder=self.root/'work/web-preview'/camera['name']/'config'
        folder.mkdir(parents=True)
        (self.root/'inventory.json').write_text(json.dumps(original))
        self.start()
        for old_revision in (legacy,revision):
            for field in ('pairing','depth_unit_policy'):
                with self.subTest(revision=old_revision,field=field):
                    changed=copy.deepcopy(original)
                    if field=='pairing':changed['rust_capture']['pairing']['clock']='system'
                    else:changed['rust_capture']['depth_unit_policy']='require-mm'
                    (folder/'revision').write_text(old_revision)
                    (self.root/'inventory.json').write_text(json.dumps(changed))
                    self.assertIn('CONFIRMATION_STALE',self.error(400,'/api/confirm',{'role':camera['name']})['error'])
                    saved=json.loads((self.root/'inventory.json').read_text())
                    self.assertFalse(saved['cameras'][0]['profiles_verified'])
        # Rejection occurs before Docker/health, so no untested policy gains proof.
        self.assertFalse((self.root/'docker-calls.jsonl').exists())
    def test_two_root_managers_preserve_each_others_preview_on_shutdown(self):
        other=self.root/'second-root'
        other.mkdir()
        shutil.copytree(self.root/'web',other/'web')
        shutil.copytree(self.root/'patches',other/'patches')
        (other/'inventory.json').write_text(json.dumps(self.data))
        item={'Id':'b'*64,'Name':'/orbbec-web-preview-camera_a',
              'Config':{'Labels':{'orbbec.web.preview':'orbbec-multicam'}},
              'State':{'Running':True},'Mounts':[{'Source':str(other.resolve()/'work/web-preview/camera_a/config'),'Destination':'/config'}]}
        for root in (self.root,other):
            (root/'inspect-fixture.json').write_text(json.dumps({'ids':['b'*64],'values':[item]}))
        self.start()
        second=subprocess.Popen([str(BINARY),'--root',str(other),'serve','--port','0'],env=self.env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        try:
            self.assertIn('http://127.0.0.1:',second.stdout.readline())
            self.process.terminate();self.process.wait(timeout=10)
            self.process.stdout.close();self.process=None
            self.assertIsNone(second.poll())
            calls=[json.loads(line) for line in (self.root/'docker-calls.jsonl').read_text().splitlines()]
            self.assertFalse(any(call and call[0] in ('stop','rm') for call in calls))
        finally:
            second.terminate();second.wait(timeout=10);second.stdout.close()
        calls=[json.loads(line) for line in (other/'docker-calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(call and call[0]=='stop' for call in calls))
        self.assertTrue(any(call and call[0]=='rm' for call in calls))
    def test_foreign_preview_cannot_be_stopped_or_confirmed(self):
        item={'Id':'b'*64,'Name':'/orbbec-web-preview-camera_a',
              'Config':{'Labels':{'orbbec.web.preview':'orbbec-multicam'}},
              'State':{'Running':True},'Mounts':[{'Source':str(self.root.resolve()/'other/work/web-preview/camera_a/config'),'Destination':'/config'}]}
        (self.root/'inspect-fixture.json').write_text(json.dumps({'ids':['b'*64],'values':[item]}))
        camera={k:v for k,v in self.data['cameras'][0].items() if k not in ('verification','profiles_verified')}
        folder=self.root/'work/web-preview/camera_a/config';folder.mkdir(parents=True)
        shared={k:v for k,v in self.data.items() if k!='cameras'}
        (folder/'revision').write_text(hashlib.sha256(json.dumps({'shared':shared,'camera':camera},sort_keys=True,separators=(',',':')).encode()).hexdigest())
        self.start()
        for endpoint in ('/api/preview/stop','/api/confirm'):
            self.assertIn('INSTANCE_OWNERSHIP',self.error(400,endpoint,{'role':'camera_a'})['error'])
        calls=[json.loads(line) for line in (self.root/'docker-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any(call and call[0] in ('stop','rm') for call in calls))
    def test_shutdown_releases_only_previews_mounted_from_this_root(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                source=(self.root.resolve() if owned else self.root.resolve()/'foreign-deployment')/'work/web-preview/camera_a/config'
                item={'Id':'b'*64,'Name':'/orbbec-web-preview-camera_a',
                      'Config':{'Labels':{'orbbec.web.preview':'orbbec-multicam'}},
                      'State':{'Running':True},'Mounts':[{'Source':str(source),'Destination':'/config'}]}
                (self.root/'inspect-fixture.json').write_text(json.dumps({'ids':['b'*64],'values':[item]}))
                self.start()
                self.process.terminate();self.process.wait(timeout=10)
                self.process.stdout.close();self.process=None
                calls=[json.loads(line) for line in (self.root/'docker-calls.jsonl').read_text().splitlines()]
                stops=[call for call in calls if call and call[0] in ('stop','rm')]
                self.assertEqual(bool(stops),owned)
                self.assertTrue(all('b'*64 in call for call in stops))
                (self.root/'docker-calls.jsonl').unlink()
    def test_generated_bundle_and_tamper_detection(self):
        result=json.loads(self.cli('render','--draft').stdout);self.assertEqual(result['camera_count'],0)
        pointer=json.loads((self.root/'state/current-generation.json').read_text())
        folder=self.root/'state/generations'/pointer['generation']
        compose=json.loads((folder/'compose.yaml').read_text())
        self.assertEqual(compose['services']['web']['command'][0],'geminidepthport')
        self.cli('status')
        (folder/'compose.yaml').write_text('{}')
        self.assertIn('GENERATION_DRIFT',self.cli('status',ok=False).stderr)
    def test_damaged_generation_recovers_only_when_stopped(self):
        for damage in ('changed', 'missing', 'pointer'):
            self.cli('render','--draft')
            pointer_path=self.root/'state/current-generation.json'
            old=json.loads(pointer_path.read_text())
            compose=self.root/'state/generations'/old['generation']/'compose.yaml'
            if damage=='changed':compose.write_text('{}')
            elif damage=='missing':compose.unlink()
            else:pointer_path.write_text('{broken')
            damaged_pointer=pointer_path.read_bytes()
            (self.root/'foreign').touch()
            self.cli('render','--draft',ok=False)
            self.assertEqual(pointer_path.read_bytes(),damaged_pointer)
            self.assertEqual(compose.exists(),damage!='missing')
            (self.root/'foreign').unlink()
            self.cli('render','--draft')
            self.assertNotEqual(json.loads(pointer_path.read_text())['generation'],old['generation'])
            self.cli('status')
    def test_unconfigured_boot_and_cross_process_stop_ownership(self):
        self.assertIn('Management is available',self.cli('boot').stdout)
        calls=self.root/'docker-calls.jsonl';self.assertFalse(calls.exists())
        self.cli('up',ok=False)
        self.assertEqual(json.loads(self.cli('down').stdout)['status'],'stopped')
        (self.root/'foreign').touch()
        self.assertIn('INSTANCE_OWNERSHIP',self.cli('down',ok=False).stderr)
        history=[json.loads(line) for line in calls.read_text().splitlines()]
        self.assertFalse(any(a[0] in ('stop','rm') for a in history))
    def test_cli_bind_set_invalidates_and_rejects_unverified_confirmation(self):
        self.cli('set','camera_a','color_exposure','200')
        self.assertEqual(json.loads((self.root/'inventory.json').read_text())['cameras'][0]['parameters']['color_exposure'],200)
        self.cli('bind','camera_a','SYNTHETIC')
        data=json.loads((self.root/'inventory.json').read_text());self.assertEqual(data['cameras'][0]['serial_number'],'SYNTHETIC')
        self.cli('set','camera_a','serial_number','"MANAGED"',ok=False)
        self.cli('confirm-profile','camera_a',ok=False)
    def test_stop_does_not_depend_on_inventory(self):
        (self.root/'inventory.json').unlink()
        self.assertEqual(json.loads(self.cli('down').stdout)['status'],'stopped')
        self.assertEqual(json.loads(self.cli('stack-stop','--remove').stdout)['status'],'stopped')
    def test_three_camera_bundle_has_correct_ros_contract(self):
        oracle=json.loads((BASE/'tests/rust/contracts.json').read_text())
        data=next(c['data'] for c in oracle['cases'] if c['name']=='three confirmed')
        (self.root/'inventory.json').write_text(json.dumps(data))
        self.assertEqual(json.loads(self.cli('render').stdout)['camera_count'],3)
        pointer=json.loads((self.root/'state/current-generation.json').read_text())
        folder=self.root/'state/generations'/pointer['generation']
        compose=json.loads((folder/'compose.yaml').read_text())
        previous=None
        for camera in sorted(data['cameras'],key=lambda c:c['name']):
            name=camera['name'];service=compose['services']['camera_'+name]
            self.assertEqual(service['labels']['orbbec.generation'],pointer['generation'])
            self.assertEqual(service['ipc'],'private')
            evidence=folder/'evidence'/name
            self.assertEqual(evidence.stat().st_mode & 0o777,0o700)
            self.assertTrue(any(isinstance(v,dict) and v.get('source')==str(evidence.resolve())
                                and v.get('target')=='/tmp/capture-observer' and v.get('read_only') is False
                                for v in service['volumes']))
            self.assertEqual((evidence/'sessions').stat().st_mode & 0o777,0o700)
            self.assertEqual((evidence/'snapshots').stat().st_mode & 0o777,0o700)
            if previous:self.assertEqual(service['depends_on'],{previous:{'condition':'service_healthy'}})
            previous='camera_'+name
            config=json.loads((folder/'config'/name/'camera.yaml').read_text())
            health=json.loads((folder/'config'/name/'health.json').read_text())
            self.assertEqual(config['color_format'],'YUYV')
            self.assertEqual(config['color_qos'],'DEFAULT')
            self.assertEqual(config['camera_name'],camera['frame_prefix'])
            self.assertTrue(all(s['required_reliability']=='reliable' for s in health['streams']))
        if os.environ.get('GDP_REAL_COMPOSE')=='1':
            subprocess.run(['docker','compose','-f',str(folder/'compose.yaml'),'--profile','cameras','config','--quiet'],env=dict(os.environ,ORBBEC_DEPLOY_DIR=str(self.root)),check=True)
    def test_historical_summary_real_cli_accepts_paired_options_without_inventory_or_docker(self):
        generation='c'*32;session='a'*32
        folder=self.root/'state/generations'/generation
        for path in [self.root/'state',self.root/'state/generations',folder,folder/'config',
                     folder/'config/cam_a',folder/'evidence',folder/'evidence/cam_a',
                     folder/'evidence/cam_a/quality',folder/'evidence/cam_a/quality'/session]:
            path.mkdir(exist_ok=True);path.chmod(0o700)
        config=folder/'config/cam_a/capture.json';config.write_text('{}');config.chmod(0o600)
        manifest=folder/'manifest.json'
        manifest.write_text(json.dumps({'kind':'geminidepthport-generation-v1','generation':generation,
            'quality_evidence_version':1,'capture_backend':'rust','cameras':[{'name':'cam_a'}],
            'files':{'config/cam_a/capture.json':hashlib.sha256(config.read_bytes()).hexdigest()}}))
        manifest.chmod(0o600)
        (self.root/'inventory.json').unlink()
        for options in [('--generation',generation,'--session',session),('--session',session,'--generation',generation)]:
            result=json.loads(self.cli('quality-session-summary','cam_a',*options).stdout)
            self.assertEqual(result['session_id'],session)
            self.assertEqual(result['generation_id'],generation)
            self.assertEqual(result['state'],'incomplete')
            self.assertFalse(result['live']);self.assertFalse(result['worker_ready'])
        for options in [('--generation',generation),('--session',session),
                        ('--generation',generation,'--session'),
                        ('--generation',generation,'--generation',generation,'--session',session),
                        ('--unknown','value')]:
            self.assertIn('ARGUMENT',self.cli('quality-session-summary','cam_a',*options,ok=False).stderr)
        self.assertFalse((self.root/'docker-calls.jsonl').exists())

    def test_formal_four_cameras_still_rejected_before_generation(self):
        self.data['cameras'].append(copy.deepcopy(self.data['cameras'][0]))
        (self.root/'inventory.json').write_text(json.dumps(self.data))
        self.assertIn('CAMERA_COUNT',self.cli('validate','--draft',ok=False).stderr)
        self.assertFalse((self.root/'state/current-generation.json').exists())
    def test_python_created_generations_are_readable(self):
        sys.path.insert(0,str(BASE));sys.path.insert(0,str(BASE/'tests'))
        from unittest.mock import patch
        import legacy_support
        import cameractl as ctl
        from test_configuration_v2 import fixture,confirmed
        data=confirmed(fixture());(self.root/'inventory.json').write_text(json.dumps(data))
        with patch.object(ctl,'ROOT',self.root),patch.object(ctl,'INV',self.root/'inventory.json'),patch.object(ctl,'assert_stopped'),patch.object(ctl,'validate_bundle'):
            ctl.render(data)
        self.cli('status')
        self.cli('validate')
    def test_shutdown_cancels_worker_and_releases_shared_lock(self):
        import time
        (self.root/'slow-build').touch()
        self.start();self.request('/api/job',{'action':'build'})
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=json.loads(self.request('/api/state'))
            if 'build-started' in state['job']['log']:break
            time.sleep(.05)
        self.assertEqual(state['job']['status'],'running')
        self.assertIn('build-started',state['job']['log'])
        self.process.terminate();self.process.wait(timeout=10)
        self.assertEqual(json.loads((self.root/'state/latest-job.json').read_text())['status'],'failed')
        self.cli('down')
    def test_stop_help_and_unknown_options_never_reach_docker(self):
        self.cli('down','--help')
        self.assertFalse((self.root/'docker-calls.jsonl').exists())
        self.cli('down','--force',ok=False)
        self.cli('stack-stop','--remvoe',ok=False)
        self.assertFalse((self.root/'docker-calls.jsonl').exists())
    def test_state_handles_malformed_external_inventory(self):
        self.start()
        (self.root/'inventory.json').write_text('{"cameras":null}')
        self.error(400,'/api/state')
        # A malformed file must not consume an HTTP worker permanently.
        self.assertEqual(json.loads(self.request('/healthz'))['status'],'ok')
    def test_legacy_web_rejects_unsupported_profiles_and_exposure(self):
        data=json.loads((BASE/'tests/fixtures/inventory.v1.json').read_text())
        for i,c in enumerate(data['cameras']):
            c['serial_number']='SYNTHETIC_'+str(i);c['profiles_verified']=False
        (self.root/'inventory.json').write_text(json.dumps(data))
        self.start()
        for key,value in [('color_format','BOGUS'),('color_fps',121),('color_exposure',1000001),('enable_color_auto_exposure',1)]:
            state=json.loads(self.request('/api/state'))
            payload={'revision':state['revision'],'cameras':[{'name':c['name'],'parameters':{key:value} if i==0 else {}} for i,c in enumerate(data['cameras'])]}
            self.error(400,'/api/config',payload)
    def test_incomplete_or_ambiguous_inspection_fails_before_stop(self):
        a='a'*64;b='b'*64
        labels={'com.docker.compose.project':'orbbec-multicam','orbbec.role':'camera','com.docker.compose.oneoff':'False'}
        item={'Id':a,'Config':{'Labels':labels},'State':{'Running':True}}
        for values in [[item,item],[dict(item,State={}),dict(item,Id=b)]]:
            (self.root/'inspect-fixture.json').write_text(json.dumps({'ids':[a,b],'values':values}))
            self.assertIn('INSTANCE_DISCOVERY',self.cli('down',ok=False).stderr)
        history=[json.loads(line) for line in (self.root/'docker-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any(a[0] in ('stop','rm') for a in history))
    def test_partial_generation_cannot_report_all_cameras_ready(self):
        import hashlib
        oracle=json.loads((BASE/'tests/rust/contracts.json').read_text())
        data=next(c['data'] for c in oracle['cases'] if c['name']=='three confirmed')
        (self.root/'inventory.json').write_text(json.dumps(data));self.cli('render')
        pointer=json.loads((self.root/'state/current-generation.json').read_text())
        folder=self.root/'state/generations'/pointer['generation'];manifest=folder/'manifest.json'
        record=json.loads(manifest.read_text());record['cameras']=record['cameras'][:1]
        record['files']={k:v for k,v in record['files'].items() if k=='compose.yaml' or k.startswith('config/camera_a/')}
        manifest.write_text(json.dumps(record));pointer['manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()
        (self.root/'state/current-generation.json').write_text(json.dumps(pointer))
        self.assertIn('GENERATION_BINDING',self.cli('check',ok=False).stderr)
    def test_image_reference_cannot_be_interpreted_as_docker_option(self):
        self.data['image']='--privileged'
        (self.root/'inventory.json').write_text(json.dumps(self.data))
        self.assertIn('CONFIG_IMAGE',self.cli('build',ok=False).stderr)
        self.assertIn('CONFIG_IMAGE',self.cli('devices',ok=False).stderr)
        self.assertFalse((self.root/'docker-calls.jsonl').exists())
    def test_single_web_owner_preserves_cli_access(self):
        self.start()
        result=self.cli('serve','--port','0',ok=False)
        self.assertIn('WEB_BUSY',result.stderr)
        self.cli('validate','--draft')
        self.request('/healthz')
    def test_device_enumeration_output_is_visible(self):
        self.assertIn('synthetic enumeration result',self.cli('devices').stdout)
    def test_http_health_accepts_fragmented_status_line(self):
        import socket,threading,time
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        def respond():
            connection,_=listener.accept()
            with connection:
                connection.recv(2048)
                for part in [b'HTTP/1.',b'1 2',b'00 OK\r\n',b'Content-Length: 0\r\nConnection: close\r\n\r\n']:
                    connection.sendall(part);time.sleep(.02)
            listener.close()
        thread=threading.Thread(target=respond);thread.start()
        try:self.cli('http-health','--port',str(listener.getsockname()[1]))
        finally:thread.join(timeout=5);listener.close()
    def test_failed_configuration_clears_stale_ready_state(self):
        (self.root/'state').mkdir()
        (self.root/'state/lifecycle.json').write_text('{"ready":true,"status":"ready"}')
        self.cli('up',ok=False)
        self.assertFalse(json.loads((self.root/'state/lifecycle.json').read_text())['ready'])
        (self.root/'state/lifecycle.json').write_text('{"ready":true,"status":"ready"}')
        self.cli('boot')
        self.assertFalse(json.loads((self.root/'state/lifecycle.json').read_text())['ready'])
    def prepare_preview(self):
        import hashlib,time
        oracle=json.loads((BASE/'tests/rust/contracts.json').read_text())
        data=copy.deepcopy(next(c['data'] for c in oracle['cases'] if c['name']=='three draft'))
        (self.root/'inventory.json').write_text(json.dumps(data))
        camera=data['cameras'][0]
        folder=self.root/'work/web-preview/camera_a';(folder/'config').mkdir(parents=True);(folder/'out').mkdir()
        def digest(data,camera):
            content={'shared':{k:v for k,v in data.items() if k!='cameras'},'camera':{k:v for k,v in camera.items() if k not in ('verification','profiles_verified')}}
            return hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        (folder/'config/revision').write_text(digest(data,camera))
        (folder/'config/lease').touch()
        (folder/'out/status.json').write_text(json.dumps({stream:{'received_at':time.time(),'frames':5,'width':camera['parameters'][stream+'_width'],'height':camera['parameters'][stream+'_height']} for stream in ['color','depth']}))
        cid='a'*64
        (self.root/'inspect-fixture.json').write_text(json.dumps({'ids':[cid],'values':[{'Id':cid,'Name':'/orbbec-web-preview-camera_a','Config':{'Labels':{'orbbec.web.preview':'orbbec-multicam'}},'State':{'Running':True},'Mounts':[{'Source':str((folder/'config').resolve()),'Destination':'/config'}]}]}))
        return data,digest
    def test_confirmation_requires_the_shared_preview_configuration(self):
        data,_=self.prepare_preview()
        data['ros_domain_id']+=1
        (self.root/'inventory.json').write_text(json.dumps(data))
        self.assertIn('CONFIRMATION_STALE',self.cli('confirm-profile','camera_a',ok=False).stderr)
        self.assertFalse(json.loads((self.root/'inventory.json').read_text())['cameras'][0]['profiles_verified'])
    def test_confirmation_does_not_overwrite_a_concurrent_file_edit(self):
        data,_=self.prepare_preview();data['cameras'][0]['calibration_revision']='external-edit'
        (self.root/'rewrite-inventory.json').write_text(json.dumps(data))
        self.assertIn('INVENTORY_DRIFT',self.cli('confirm-profile','camera_a',ok=False).stderr)
        saved=json.loads((self.root/'inventory.json').read_text())
        self.assertEqual(saved['cameras'][0]['calibration_revision'],'external-edit')
        self.assertFalse(saved['cameras'][0]['profiles_verified'])
    def test_live_confirmation_persists_matching_evidence(self):
        _,digest=self.prepare_preview();self.cli('confirm-profile','camera_a')
        saved=json.loads((self.root/'inventory.json').read_text());camera=saved['cameras'][0]
        self.assertTrue(camera['profiles_verified'])
        self.assertEqual(camera['verification']['configuration_digest'],digest(saved,camera))
    def test_web_save_preserves_an_edit_during_preview_shutdown(self):
        import hashlib
        data,_=self.prepare_preview();self.start()
        revised=copy.deepcopy(data);revised['cameras'][0]['calibration_revision']='external-edit'
        (self.root/'rewrite-inventory.json').write_text(json.dumps(revised))
        payload={'revision':hashlib.sha256((self.root/'inventory.json').read_bytes()).hexdigest(),'cameras':[{'name':c['name'],'parameters':{'color_exposure':100} if i==0 else {}} for i,c in enumerate(data['cameras'])]}
        self.assertIn('INVENTORY_DRIFT',self.error(400,'/api/config',payload)['error'])
        self.assertEqual(json.loads((self.root/'inventory.json').read_text()),revised)
    def test_confirmation_renews_the_capture_session_lease(self):
        import time
        self.prepare_preview();lease=self.root/'work/web-preview/camera_a/config/lease'
        os.utime(lease,(time.time()-124,time.time()-124))
        (self.root/'require-fresh-lease').touch()
        self.cli('confirm-profile','camera_a')
    def test_confirmation_rejects_a_replaced_preview_instance(self):
        self.prepare_preview();(self.root/'replace-preview').touch()
        self.assertIn('CONFIRMATION_STALE',self.cli('confirm-profile','camera_a',ok=False).stderr)
        self.assertFalse(json.loads((self.root/'inventory.json').read_text())['cameras'][0]['profiles_verified'])
    def test_background_job_persistence(self):
        import time
        self.start();job=json.loads(self.request('/api/job',{'action':'render'}))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=json.loads(self.request('/api/state'))
            if state['job'] and state['job']['status']!='running':break
            time.sleep(.05)
        self.assertEqual(state['job']['id'],job['job_id'])
        self.assertEqual(state['job']['status'],'failed') # Strict render rejects unverified/unbound inventory.
        saved=json.loads((self.root/'state/latest-job.json').read_text())
        self.assertEqual(saved['status'],'failed')
        self.assertFalse(json.loads(self.request('/api/state'))['generated_current'])

if __name__=='__main__':unittest.main(verbosity=2)
