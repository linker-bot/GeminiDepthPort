"""Exercise Rust Linux preflight/lifecycle with synthetic sysfs and a shell Docker double.

The test container has no network, Docker socket, or real USB devices. Its only
'docker' command is the fixture below; no camera SDK is invoked.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[2]
IMAGE=os.environ.get('GDP_TEST_IMAGE','geminidepthport-management:audit-test')

DOCKER_DOUBLE=r'''#!/bin/sh
set -eu
root=$ORBBEC_DEPLOY_DIR
cd "$root"
event() { printf '%s\n' "$1" >> events; }
identity() {
  case "$1" in
    camera_camera_a|dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa) name=camera_a; id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;;
    camera_camera_b|bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb) name=camera_b; id=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb;;
    camera_camera_c|cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc) name=camera_c; id=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc;;
    *) exit 90;;
  esac
  if [ "$name" = camera_a ] && [ -f replace-camera-a ]; then id=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd; fi
}
case "$1" in
  info) exit 0;;
  image) cat image.json; exit 0;;
  ps)
    case "$*" in *orbbec.web.preview*) exit 0;; *orbbec.role=camera*) :;; *) exit 0;; esac
    for camera in camera_a camera_b camera_c; do
      if [ -f "running-$camera" ]; then identity "camera_$camera"; printf '%s\n' "$id"; fi
    done;;
  inspect)
    shift
    generation=$(sed -n 's/.*"generation": "\([a-f0-9]*\)".*/\1/p' state/current-generation.json)
    printf '['; separator=
    for value in "$@"; do
      identity "$value"
      printf '%s{"Id":"%s","Config":{"Labels":{"com.docker.compose.project":"orbbec-multicam","com.docker.compose.oneoff":"False","orbbec.role":"camera","orbbec.generation":"%s","orbbec.camera":"%s"}},"State":{"Running":true,"Status":"running","Health":{"Status":"healthy"}}}' "$separator" "$id" "$generation" "$name"
      separator=,
    done
    printf ']\n';;
  compose)
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in config|run|up|exec|ps) operation=$1;shift;break;; esac
      shift
    done
    if [ "$operation" = config ]; then exit 0; fi
    service=
    for value in "$@"; do case "$value" in camera_camera_*) service=$value; break;; esac; done
    identity "$service"
    case "$operation" in
      run) event "validate:$name";;
      up) event "start:$name"; touch "running-$name";;
      ps) printf '%s\n' "$id";;
      exec)
        event "probe:$name"
        if [ -e "fail-probe-$name" ]; then printf 'synthetic probe failure\n' >&2;exit 1;fi
        printf '[]\n';;
    esac;;
  exec)
    case "$*" in
      *calibration_export.py*)
        event calibration:collect
        if [ -f fail-calibration ]; then exit 1; fi
        cat > /tmp/calibration-request.json
        digest=$(sha256sum /tmp/calibration-request.json | cut -d ' ' -f 1)
        for output in "$@"; do :; done
        sed "s/REQ_HASH/$digest/" observations.json > "$output"
        if [ -f replace-during-calibration ]; then touch replace-camera-a; fi;;
      *" cat "*) for output in "$@"; do :; done; cat "$output";;
      *" rm "*) event calibration:cleanup; for output in "$@"; do :; done; rm -f "$output";;
    esac;;
  run)
    identity_value=;previous=;home=0;ros_home=0
    for value in "$@"; do
      if [ "$previous" = --user ]; then identity_value=$value; fi
      if [ "$value" = HOME=/tmp ]; then home=1; fi
      if [ "$value" = ROS_HOME=/tmp/ros ]; then ros_home=1; fi
      previous=$value
    done
    [ "$identity_value" = "$(id -u):$(id -g)" ] || exit 94
    [ "$home" = 1 ] && [ "$ros_home" = 1 ] || exit 95
    [ "$(stat -c %u work/web-preview/camera_a/config/camera.yaml)" = "$(id -u)" ] || exit 96
    event preview:identity-validated
    printf 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n';;
  stop)
    for value in "$@"; do :; done
    identity "$value";event "stop:$name";rm -f "running-$name";;
  rm) exit 0;;
  *) exit 91;;
esac
'''

class LinuxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        oracle=json.loads((BASE/'tests/rust/contracts.json').read_text())
        self.data=next(c['data'] for c in oracle['cases'] if c['name']=='three confirmed')
        (self.root/'inventory.json').write_text(json.dumps(self.data))
        shutil.copytree(BASE/'web',self.root/'web')
        observations=oracle['requests'][0]['observations']
        (self.root/'observations.json').write_text(json.dumps(observations).replace(observations['request_sha256'],'REQ_HASH'))
        shutil.copytree(BASE/'patches/public-orbbec',self.root/'patches/public-orbbec')
        for directory in ['bin','usb','udev','devices','rules']:(self.root/directory).mkdir()
        (self.root/'rules/99-obsensor-libusb.rules').write_text('# synthetic rule\n')
        for index,camera in enumerate(self.data['cameras']):
            folder=self.root/'usb'/str(index);folder.mkdir()
            for key,value in [('idVendor','2bc5'),('serial',camera['serial_number']),('product','Gemini 335L' if index==0 else 'Gemini 2')]:
                (folder/key).write_text(value)
        (self.root/'bin/docker').write_text(DOCKER_DOUBLE);(self.root/'bin/docker').chmod(0o755)
        manifest=json.loads((BASE/'patches/public-orbbec/manifest.json').read_text())
        labels={'org.opencontainers.image.revision':self.data['upstream_commit'],
            'org.geminidepthport.explicit-bindings':'1','org.geminidepthport.native-yuyv':'1',
            'org.geminidepthport.native-yuyv-patch':manifest['patch_sha256'],
            'org.geminidepthport.communication-profile':'humble-fastdds-local-udp-v1'}
        (self.root/'image.json').write_text(json.dumps([{'Config':{'Labels':labels}}]))
    def tearDown(self):self.tmp.cleanup()
    def cli(self,*args,ok=True,stdin=None):
        command=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
            '--user',f'{os.getuid()}:{os.getgid()}','--tmpfs','/tmp',
            '-v',f'{self.root}:{self.root}:rw','-w',str(self.root),
            '-e',f'ORBBEC_DEPLOY_DIR={self.root}','-e',f'PATH={self.root}/bin:/usr/local/bin:/usr/bin:/bin']
        for source,target in [('usb','/sys/bus/usb/devices'),('udev','/run/udev'),('devices','/dev/bus/usb'),('rules','/etc/udev/rules.d')]:
            command+=['-v',f'{self.root/source}:{target}:ro']
        if stdin is not None:command+=['-i']
        command += [IMAGE,'geminidepthport',*args]
        result=subprocess.run(command,input=stdin,text=True,capture_output=True,timeout=40)
        if ok:self.assertEqual(result.returncode,0,result.stderr)
        else:self.assertNotEqual(result.returncode,0)
        return result
    def events(self):return (self.root/'events').read_text().splitlines()
    def test_sequential_start_reuse_recording_guard_and_stop(self):
        self.cli('render')
        self.assertTrue(json.loads(self.cli('up').stdout)['ready'])
        self.assertEqual(self.events(),['validate:camera_a','validate:camera_b','validate:camera_c',
            'start:camera_a','probe:camera_a','start:camera_b','probe:camera_b','start:camera_c','probe:camera_c',
            'probe:camera_a','probe:camera_b','probe:camera_c'])
        reused=json.loads(self.cli('up').stdout);self.assertTrue(reused['reused'])
        self.assertEqual(sum(e.startswith('start:') for e in self.events()),3)
        token=json.loads(self.cli('protect').stdout)['token']
        self.assertIn('RECORDING_PROTECTED',self.cli('down',ok=False).stderr)
        self.cli('release-protection',stdin=token)
        self.assertEqual(json.loads(self.cli('down').stdout)['status'],'stopped')
        self.assertEqual([e for e in self.events() if e.startswith('stop:')],['stop:camera_a','stop:camera_b','stop:camera_c'])
    def test_failed_middle_camera_never_starts_later_camera_or_reports_ready(self):
        self.cli('render');(self.root/'fail-probe-camera_b').touch()
        self.cli('up',ok=False)
        result=json.loads((self.root/'state/lifecycle.json').read_text())
        self.assertFalse(result['ready']);self.assertEqual(result['status'],'failed')
        self.assertEqual([c['status'] for c in result['cameras']],['healthy','failed','not_started'])
        self.assertNotIn('start:camera_c',self.events())
        self.assertTrue((self.root/'running-camera_a').exists())
        self.assertTrue((self.root/'running-camera_b').exists())
        self.assertFalse((self.root/'running-camera_c').exists())

    def test_calibration_snapshot_and_temporary_evidence_cleanup(self):
        self.cli('render');self.cli('up')
        summary=json.loads(self.cli('calibration-export').stdout)
        self.assertEqual(summary['camera_count'],3)
        self.assertTrue(summary['structural_validation']);self.assertFalse(summary['physical_accuracy_verified'])
        records=list((self.root/'state/calibration').glob('*.json'));self.assertEqual(len(records),1)
        self.assertEqual(json.loads(records[0].read_text())['snapshot_sha256'],summary['snapshot_sha256'])
        self.assertIn('calibration:cleanup',self.events())
    def test_calibration_rejects_a_recreated_driver_with_the_same_generation(self):
        self.cli('render');self.cli('up');(self.root/'replace-during-calibration').touch()
        self.cli('calibration-export',ok=False)
        self.assertFalse((self.root/'state/calibration').exists())
        self.assertIn('calibration:cleanup',self.events())
    def test_failed_calibration_collection_still_cleans_temporary_evidence(self):
        self.cli('render');self.cli('up');(self.root/'fail-calibration').touch()
        self.cli('calibration-export',ok=False)
        self.assertFalse((self.root/'state/calibration').exists())
        self.assertIn('calibration:cleanup',self.events())

    def test_preview_uses_config_owner_identity_and_writable_ros_home(self):
        import socket,time,re
        from urllib.request import Request,build_opener,ProxyHandler
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        command=['docker','run','-d','--network','bridge','--read-only','--cap-drop','ALL','--tmpfs','/tmp',
            '--user',f'{os.getuid()}:{os.getgid()}','-v',f'{self.root}:{self.root}:rw','-w',str(self.root),
            '-e',f'ORBBEC_DEPLOY_DIR={self.root}','-e','ORBBEC_CONTAINERIZED=1','-e',f'ORBBEC_PUBLIC_PORT={port}',
            '-e',f'PATH={self.root}/bin:/usr/local/bin:/usr/bin:/bin','-p',f'127.0.0.1:{port}:8765']
        for source,target in [('usb','/sys/bus/usb/devices'),('udev','/run/udev'),('devices','/dev/bus/usb'),('rules','/etc/udev/rules.d')]:command+=['-v',f'{self.root/source}:{target}:ro']
        command += [IMAGE,'geminidepthport','serve','--host','0.0.0.0']
        cid=subprocess.check_output(command,text=True).strip()
        try:
            base=f'http://127.0.0.1:{port}';opener=build_opener(ProxyHandler({}));deadline=time.monotonic()+15
            while True:
                try:
                    with opener.open(base+'/',timeout=2) as response:html=response.read().decode()
                    break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.1)
            token=re.search(r'name="csrf-token" content="([^"]+)"',html).group(1)
            with opener.open(Request(base+'/api/preview/start',data=b'{"role":"camera_a"}',headers={'Content-Type':'application/json','X-CSRF-Token':token,'Origin':base}),timeout=15) as response:
                self.assertIn('message',json.loads(response.read()))
            self.assertIn('preview:identity-validated',self.events())
        finally:subprocess.run(['docker','rm','-f',cid],capture_output=True,check=True)

if __name__=='__main__':unittest.main(verbosity=2)
