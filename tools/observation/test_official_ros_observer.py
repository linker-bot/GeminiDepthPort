import importlib.util
import pathlib
import unittest
from types import SimpleNamespace as N

spec = importlib.util.spec_from_file_location("adapter", pathlib.Path(__file__).with_name("official_ros_observer.py"))
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class DecoderTests(unittest.TestCase):
    def test_config_rejects_duplicate_keys_at_every_nesting_level(self):
        for raw in ('{"engine_config":{},"engine_config":{}}',
                    '{"engine_config":{"streams":{"color":{"mapper":1,"mapper":2}}}}'):
            with self.assertRaisesRegex(ValueError, "CONFIG_DUPLICATE_KEY"):
                adapter.parse_config(raw)

    def test_config_rejects_nonfinite_constants(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaisesRegex(ValueError, "CONFIG_NONFINITE"):
                adapter.parse_config('{"engine_config":{"nested":[' + constant + ']}}')
        self.assertEqual(adapter.parse_config('{"engine_config":{"nested":[1,null]}}'),
                         {"engine_config": {"nested": [1, None]}})

    def test_image_decodes_length_without_copying_or_exporting_pixels(self):
        class Pixels:
            def __len__(self):
                return 8
            def __iter__(self):
                raise AssertionError("pixels must not be read")
        message = N(header=N(stamp=N(sec=12, nanosec=3), frame_id="synthetic_color"),
                    width=2, height=2, step=4, encoding="yuyv", is_bigendian=0, data=Pixels())
        result = adapter.image(message)
        self.assertEqual(result["payload_len"], 8)
        self.assertNotIn("sequence", result)
        self.assertNotIn("data", result)

    def test_camera_info_casts_numeric_arrays_and_preserves_geometry(self):
        message = N(header=N(stamp=N(sec=12, nanosec=3), frame_id="synthetic_color"),
                    width=2, height=2, distortion_model="plumb_bob", k=range(9),
                    d=[0]*5, r=range(9), p=range(12), binning_x=0, binning_y=0,
                    roi=N(x_offset=0,y_offset=0,width=0,height=0,do_rectify=False))
        result = adapter.camera_info(message)
        self.assertIs(type(result["k"][0]), float)
        self.assertEqual(result["p"], list(map(float, range(12))))

    def test_unknown_metadata_does_not_invent_stamp_or_sdk_fields(self):
        result = adapter.metadata(N(data='{"vendor":"synthetic"}'))
        self.assertEqual(result["stamp"], {"sec":0,"nanosec":0})
        self.assertNotIn("sequence", result)
        result = adapter.metadata(N(data='x'*5000))
        self.assertEqual(result["data"], "")


if __name__ == "__main__":
    unittest.main()
