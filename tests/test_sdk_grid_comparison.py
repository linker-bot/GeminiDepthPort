"""Synthetic grid/unit regressions; no SDK/device or physical certification."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tools.validation import d2c_compare as comparison

PROGRAM=Path(os.environ.get('GDP_DESCRIPTOR_WARP',Path(__file__).resolve().parents[1]/'target/debug/examples/descriptor_warp'))


def model(distorted=False):
    return dict(width=2,height=1,fx=1.0,fy=1.0,cx=0.0,cy=0.0,skew=0.0,
                distortion_model='plumb_bob' if distorted else 'none',
                distortion_coefficients=[1000.0,0.0,0.0,0.0,0.0] if distorted else [])


class SdkGridComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.descriptor=dict(version=1,association='sdk_frameset',hardware_sync_verified=False,physical_accuracy_verified=False,
            color=model(True),depth=model(),target=model(),target_grid='sdk-aligned-output-profile',
            target_transform_source='depth-profile-to-sdk-aligned-profile',rotation_row_major=[1.,0.,0.,0.,1.,0.,0.,0.,1.],
            translation_m=[0.,0.,0.],depth_stride=4,depth_endian='little',source_scale_mm=1.,sdk_aligned_scale_mm=1.,
            color_sequence=7,depth_sequence=15,color_device_us=1000,depth_device_us=1000,
            selected_clock='device',selected_color_timestamp_us=1000,selected_depth_timestamp_us=1000,max_pair_delta_us=20000)
        (self.root/'CAPTURE_COMPLETE').write_text('synthetic only\n')
        for name in ('depth.raw16','sdk-aligned.raw16'):(self.root/name).write_bytes(struct.pack('<HH',1000,1000))
        (self.root/'color.yuyv').write_bytes(b'\x01\x02\x03\x04')
        self.save()
    def save(self):(self.root/'descriptor.json').write_text(json.dumps(self.descriptor,allow_nan=False))
    def test_nonfinite_scale_and_json_are_not_accepted(self):
        for raw in ('{"x":NaN}','{"x":1e999}','{"x":{"a":1,"a":2}}'):
            with self.assertRaises(ValueError):comparison.strict(raw)
        with self.assertRaises(ValueError):comparison.metrics([1],[1],True)
        with self.assertRaises(ValueError):comparison.metrics([1],[1],0.)
    def test_legacy_original_color_grid_cannot_be_mislabeled_comparable(self):
        del self.descriptor['target_grid'];self.save()
        with self.assertRaisesRegex(ValueError,'EXPLICIT_SDK_TARGET_GRID_REQUIRED'):
            comparison.compare(self.root,str(PROGRAM))
    @unittest.skipUnless(PROGRAM.exists(),'build descriptor_warp example first')
    def test_actual_scalar_cli_uses_sdk_output_grid_not_original_color_distortion(self):
        result=comparison.compare(self.root,str(PROGRAM))
        self.assertTrue(result['complete']);self.assertFalse(result['physical_accuracy_verified'])
        self.assertEqual(result['target_grid'],'sdk-aligned-output-profile')
        self.assertEqual(result['differences']['common_exact_equal'],2)
        self.assertEqual(struct.unpack('<HH',(self.root/'scalar-color-z.raw16').read_bytes()),(1000,1000))
        self.assertEqual((self.root/'scalar-color-z.raw16').stat().st_mode & 0o777,0o600)
    @unittest.skipUnless(PROGRAM.exists(),'build descriptor_warp example first')
    def test_sdk_millimetre_overflow_is_failure_not_complete_partial_metrics(self):
        self.descriptor['sdk_aligned_scale_mm']=100.;self.save()
        result=comparison.compare(self.root,str(PROGRAM))
        self.assertFalse(result['complete']);self.assertEqual(result['code'],'SDK_MM_CONVERSION_OVERFLOW')
        self.assertEqual(result['differences']['sdk_mm_overflow'],2)
    def test_failed_child_cannot_claim_success_and_input_mutation_is_rejected(self):
        with patch.object(comparison.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'{"complete":true}')):
            with self.assertRaisesRegex(ValueError,'SCALAR_RESULT_MISMATCH'):comparison.compare(self.root,str(PROGRAM))
        def change(*args,**kwargs):
            (self.root/'depth.raw16').write_bytes(b'\0'*4)
            return SimpleNamespace(returncode=1,stdout=b'{"complete":false}')
        with patch.object(comparison.subprocess,'run',side_effect=change):
            with self.assertRaisesRegex(ValueError,'SOURCE_CHANGED_DURING_COMPARISON'):comparison.compare(self.root,str(PROGRAM))


if __name__=='__main__':unittest.main()
