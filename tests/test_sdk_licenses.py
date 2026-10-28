"""SDK license collection regression; no native library or device access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sdk_license_tools', BASE / 'scripts/capture-runtime/sdk_licenses.py')
licenses = importlib.util.module_from_spec(spec)
spec.loader.exec_module(licenses)


class SdkLicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'upstream'
        for name in licenses.REQUIRED - {'SDK-LICENSE.txt', 'SDK-LICENSE-SOURCE.json'}:
            if name.startswith('sdk-licenses/'):
                path = self.source / 'orbbec_camera/SDK/licenses' / name.removeprefix('sdk-licenses/')
            else:
                path = self.source / name.removeprefix('ROS2-')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('Synthetic license: ' + name + '\n').encode())
        self.vendor = BASE / 'licenses/orbbec-sdk-v2.10.6'
        self.output = self.root / 'output'

    def collect(self):
        return licenses.collect(self.source, self.vendor, self.output)

    def test_entire_tree_including_copying_and_unusual_names_is_preserved(self):
        extra = self.source / 'orbbec_camera/SDK/licenses/nested/TERMS.data'
        extra.parent.mkdir()
        extra.write_bytes(b'Synthetic terms\r\nincluding original bytes\n')
        record = self.collect()
        self.assertEqual(record, licenses.verify(self.output))
        self.assertEqual((self.output / 'sdk-licenses/nested/TERMS.data').read_bytes(), extra.read_bytes())
        for name in ('libusb/COPYING', 'live555/COPYING', 'live555/COPYING.LESSER'):
            self.assertIn('sdk-licenses/' + name, record['files'])
        self.assertEqual((self.output / 'SDK-LICENSE.txt').read_bytes(), (self.vendor / 'LICENSE.txt').read_bytes())

    def test_missing_required_upstream_file_fails_before_output_creation(self):
        (self.source / 'orbbec_camera/SDK/licenses/live555/COPYING.LESSER').unlink()
        with self.assertRaisesRegex(ValueError, 'INCOMPLETE'):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_missing_pinned_license_is_rejected(self):
        with self.assertRaises(ValueError):
            licenses.collect(self.source, self.root / 'absent', self.output)
        self.assertFalse(self.output.exists())

    def test_modified_pinned_license_and_source_are_rejected(self):
        other = self.root / 'sdk-license'
        other.mkdir()
        (other / 'LICENSE.txt').write_bytes((self.vendor / 'LICENSE.txt').read_bytes() + b'changed')
        (other / 'source.json').write_bytes((self.vendor / 'source.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'SOURCE_MISMATCH'):
            licenses.collect(self.source, other, self.output)
        (other / 'LICENSE.txt').write_bytes((self.vendor / 'LICENSE.txt').read_bytes())
        (other / 'source.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'SOURCE_MISMATCH'):
            licenses.collect(self.source, other, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_packaged_file_is_rejected(self):
        self.collect()
        (self.output / 'sdk-licenses/libusb/COPYING').unlink()
        with self.assertRaisesRegex(ValueError, 'MISSING_OR_INVALID'):
            licenses.verify(self.output)

    def test_tampered_copying_is_rejected(self):
        self.collect()
        (self.output / 'sdk-licenses/live555/COPYING').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            licenses.verify(self.output)

    def test_inventory_cannot_silently_drop_required_license(self):
        self.collect()
        path = self.output / 'license-inventory.json'
        record = json.loads(path.read_text())
        del record['files']['sdk-licenses/libusb/COPYING']
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'INCOMPLETE'):
            licenses.verify(self.output)

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        (self.output / 'sentinel').write_text('unchanged')
        with self.assertRaises(FileExistsError):
            self.collect()
        self.assertEqual((self.output / 'sentinel').read_text(), 'unchanged')

    def test_license_symlink_is_not_followed(self):
        path = self.source / 'orbbec_camera/SDK/licenses/libusb/COPYING'
        path.unlink()
        path.symlink_to(self.vendor / 'LICENSE.txt')
        with self.assertRaisesRegex(ValueError, 'SYMLINK'):
            self.collect()
        self.assertFalse(self.output.exists())

    def test_unlisted_packaged_file_is_rejected(self):
        self.collect()
        (self.output / 'extra.txt').write_text('unlisted')
        with self.assertRaisesRegex(ValueError, 'FILE_SET'):
            licenses.verify(self.output)


if __name__ == '__main__':
    unittest.main()
