"""Numeric comparison semantics only; no SDK, ROS or physical accuracy claim."""
import unittest
from tools.validation.d2c_compare import metrics, samples, strict


class SdkDifferenceTests(unittest.TestCase):
    def test_equal_units_and_common_mask_are_separate(self):
        result = metrics([1000, 0, 20, 0], [10000, 10, 0, 0], .1)
        self.assertEqual(result['common_valid'], 1)
        self.assertEqual(result['common_exact_equal'], 1)
        self.assertEqual(result['scalar_only'], 1)
        self.assertEqual(result['sdk_only'], 1)
        self.assertEqual(result['common_abs_difference_mm']['maximum'], 0)

    def test_sdk_rounding_preserves_zero_and_rejects_range_in_statistics(self):
        result = metrics([0, 1, 65535], [0, 1, 65535], 2.)
        self.assertEqual(result['sdk_mm_overflow'], 1)
        self.assertEqual(result['common_abs_difference_mm']['maximum'], 1)
        self.assertEqual(metrics([1], [1], .1)['common_exact_equal'], 1)

    def test_unknown_geometry_and_duplicate_input_are_not_silently_assumed(self):
        with self.assertRaises(ValueError):
            strict(b'{"depth":1,"depth":2}')
        with self.assertRaises(ValueError):
            strict(b'{"depth":NaN}')
        with self.assertRaises(ValueError):
            samples(b'\x00', 1)
        self.assertEqual(list(samples(b'\x01\x00\xff\xff', 2)), [1, 65535])
