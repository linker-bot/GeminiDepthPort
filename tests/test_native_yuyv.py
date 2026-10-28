"""Public patch tooling and synthetic memory/preview contracts; no camera fixtures."""
import copy
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import driver_build
import configuration
import cameractl as ctl
from test_configuration_v2 import fixture

spec = importlib.util.spec_from_file_location('public_patch', BASE/'scripts/apply_public_yuyv_patch.py')
tooling = importlib.util.module_from_spec(spec); spec.loader.exec_module(tooling)
sha = lambda content: hashlib.sha256(content).hexdigest()


class PublicPatchTests(unittest.TestCase):
    def setUp(self):
        # Nest in the checkout to exercise Git's parent-repository discovery hazard.
        (BASE/'work').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=BASE/'work')
        self.root = Path(self.tmp.name); self.source=self.root/'source'; self.source.mkdir()
        self.bundle=self.root/'bundle'; self.bundle.mkdir()
        (self.source/'example.cpp').write_text('original public fixture\n')
        change=''.join(difflib.unified_diff(['original public fixture\n'],['patched public fixture\n'],
                                         fromfile='a/example.cpp',tofile='b/example.cpp'))
        (self.bundle/'test.patch').write_text(change)
        self.manifest={'upstream_commit':'synthetic-public-baseline','patch_file':'test.patch','patch_sha256':sha(change.encode()),
                       'original_files':{'example.cpp':sha(b'original public fixture\n')},
                       'patched_files':{'example.cpp':sha(b'patched public fixture\n')}}
        self.persist()

    def tearDown(self):self.tmp.cleanup()
    def persist(self):(self.bundle/'manifest.json').write_text(json.dumps(self.manifest))
    def process(self,action,revision='synthetic-public-baseline'):
        return tooling.process(self.source,action,revision,self.bundle)

    def test_check_apply_verify_and_repeated_apply_rejection(self):
        self.process('check');self.assertEqual((self.source/'example.cpp').read_text(),'original public fixture\n')
        result=self.process('apply');self.assertFalse(result['hardware_verified'])
        self.assertEqual((self.source/'example.cpp').read_text(),'patched public fixture\n')
        self.process('verify')
        with self.assertRaises(ValueError):self.process('apply')

    def test_unknown_baseline_and_modified_source_are_rejected(self):
        with self.assertRaises(ValueError):self.process('apply','other-public-baseline')
        (self.source/'example.cpp').write_text('unrecognized source')
        with self.assertRaises(ValueError):self.process('apply')

    def test_modified_patch_and_output_are_rejected(self):
        (self.bundle/'test.patch').write_text('invalid patch')
        with self.assertRaises(ValueError):self.process('apply')
        with self.assertRaises(ValueError):self.process('verify')

    def test_invalid_patch_action_is_not_a_mutating_fallback(self):
        with self.assertRaises(ValueError):self.process('invalid')
        self.assertEqual((self.source/'example.cpp').read_text(),'original public fixture\n')

    def test_expected_output_mismatch_is_rejected(self):
        self.manifest['patched_files']['example.cpp']=sha(b'different output');self.persist()
        with self.assertRaises(ValueError):self.process('apply')


class PublicBuildTests(unittest.TestCase):
    def test_manifest_identifies_exact_public_source_and_patch(self):
        record=driver_build.patch_identity()
        profile=json.loads((BASE/'driver_profiles.json').read_text())
        self.assertEqual(record['upstream_commit'],profile['upstream_commit'])
        helper=BASE/'patches/public-orbbec/native_yuyv.hpp'
        self.assertEqual(record['patched_files']['orbbec_camera/include/orbbec_camera/native_yuyv.hpp'],sha(helper.read_bytes()))
        dockerfile=(BASE/'Dockerfile').read_text()
        self.assertIn('ARG YUYV_PATCH_SHA256='+record['patch_sha256'],dockerfile)
        self.assertLess(dockerfile.index('--action apply'),dockerfile.index('colcon build'))
        self.assertLess(dockerfile.index('ros_native_yuyv_smoke.py\nLABEL'),dockerfile.index('LABEL org.geminidepthport.native-yuyv='))

    def test_cpp_sanitized_owned_buffer_layout(self):
        compiler=shutil.which('clang++') or shutil.which('g++')
        if not compiler:self.skipTest('C++ compiler unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);include=root/'include/orbbec_camera';include.mkdir(parents=True)
            shutil.copyfile(BASE/'patches/public-orbbec/native_yuyv.hpp',include/'native_yuyv.hpp')
            subprocess.run([compiler,'-std=c++17','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                            '-fno-omit-frame-pointer','-I'+str(root/'include'),str(BASE/'tests/cpp/native_yuyv_buffer_test.cpp'),
                            '-o',str(root/'buffer-test')],check=True,capture_output=True,text=True)
            result=subprocess.run([str(root/'buffer-test')],check=True,capture_output=True,text=True)
            self.assertIn('passed',result.stdout)

    def test_unvalidated_processing_cannot_be_requested_for_native_color(self):
        for key in ('enable_color_undistortion','color_flip','color_rotation','enable_enhanced_depth'):
            data=fixture();data['cameras'][0]['parameters'][key]=True
            with self.assertRaises(configuration.ConfigurationError):configuration.validate_inventory(data,draft=True)
