"""Exercise the actual Linux lifecycle shell-double schema without Docker/USB."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('linux_lifecycle_fixture',BASE/'tests/rust/test_linux_lifecycle.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
class LinuxObserverFixtureTests(unittest.TestCase):
    def test_shell_double_matches_bound_live_observer_quality_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);generation='d'*32
            folder=root/'state/generations'/generation/'config/camera_a';folder.mkdir(parents=True)
            (folder/'observer-policy.json').write_text('{}')
            (root/'state/current-generation.json').write_text(json.dumps({'generation':generation}))
            docker=root/'docker';docker.write_text(fixture.DOCKER_DOUBLE);docker.chmod(0o755)
            result=subprocess.run([str(docker),'exec','a'*64,'python3','-c','backend=sys.argv[1]','official'],env=dict(os.environ,ORBBEC_DEPLOY_DIR=str(root),PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH','')),capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout);status=value['status'];quality=value['current']['quality']
            self.assertTrue(value['processes_alive'])
            self.assertEqual(status['kind'],'official-observer-status')
            self.assertEqual(status['session_kind'],'observer');self.assertFalse(status['native_session_verified'])
            self.assertEqual(quality['session_id'],status['session_id'])
            self.assertEqual(quality['at']['session_id'],status['session_id'])
            self.assertFalse(quality['current_session_finalized'])
            self.assertEqual(quality['latest_event_sequence'],value['events']['latest_sequence'])
            self.assertEqual(value['current']['snapshot_id'],status['snapshot_id'])
if __name__=='__main__':unittest.main()
