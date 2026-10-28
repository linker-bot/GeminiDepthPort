"""Keep onboarding examples public and their repository navigation usable."""
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def check_public_inventory(value):
    """Reject runtime proof/identity, including nested additions to a draft."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"serial_number", "calibration_revision"} and child != "":
                raise ValueError("public example contains a deployment binding")
            if key in {"enabled", "profiles_verified", "physical_accuracy_verified", "hardware_sync_verified"} and child is not False:
                raise ValueError("public example claims activation or verification")
            if key == "verification" and child is not None:
                raise ValueError("public example contains confirmation evidence")
            if key in {"usb_recovery", "runtime_digest", "configuration_digest", "session_id", "snapshot_id", "protection_token"}:
                raise ValueError("runtime artifacts must be generated, not copied")
            check_public_inventory(child)
    elif isinstance(value, list):
        for child in value:
            check_public_inventory(child)
    elif isinstance(value, str):
        if re.search(r"(?:/Users/|/home/|192\.168\.|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.)", value):
            raise ValueError("public example contains a private path or LAN address")


class PublicExamples(unittest.TestCase):
    def test_inventory_privacy(self):
        paths = [ROOT / "inventory.v2.example.json", *sorted((ROOT / "examples").glob("*/inventory.json"))]
        self.assertGreaterEqual(len(paths), 3)
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                check_public_inventory(json.loads(path.read_text()))

    def test_privacy_check_rejects_nested_runtime_evidence(self):
        for bad in ({"serial_number": "REAL_BINDING"}, {"verification": {}},
                    {"runtime_digest": "0" * 64}, {"physical_accuracy_verified": True},
                    {"path": "/home/operator/private.json"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_public_inventory({"nested": [bad]})

    def test_documented_shell_blocks_parse_without_execution(self):
        blocks = 0
        for path in sorted((ROOT / "examples").rglob("*.md")):
            for block in re.findall(r"```sh\n(.*?)```", path.read_text(), re.DOTALL):
                blocks += 1
                with self.subTest(document=path.relative_to(ROOT)):
                    result = subprocess.run(["bash", "-n"], input=block, text=True,
                                            capture_output=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(blocks, 0)

    def test_example_document_links_resolve_within_repository(self):
        documents = sorted((ROOT / "examples").rglob("*.md"))
        self.assertTrue(documents, "examples need onboarding documentation")
        for path in documents:
            for target in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", path.read_text()):
                if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                target = target.split("#", 1)[0]
                resolved = (path.parent / target).resolve()
                with self.subTest(document=path.relative_to(ROOT), target=target):
                    self.assertTrue(resolved.is_relative_to(ROOT))
                    self.assertTrue(resolved.exists(), f"broken onboarding link: {target}")


if __name__ == "__main__":
    unittest.main()
