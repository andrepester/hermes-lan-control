"""Release boundary checks; no plugin imports, network traffic, or Git writes."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_release import build
from check_package import PACKAGE_FILES, check


class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        for name in PACKAGE_FILES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def allow_release(self):
        path = self.root / "plugin.yaml"
        manifest = yaml.safe_load(path.read_text())
        # Synthetic fixture value; this makes no real compatibility claim.
        manifest["requires_hermes"] = ">=999.0.0"
        path.write_text(yaml.safe_dump(manifest))

    def test_release_requires_verified_compatibility(self):
        path = self.root / "plugin.yaml"
        manifest = yaml.safe_load(path.read_text())
        for floor in (None, "", "  ", False, 123):
            with self.subTest(floor=floor):
                manifest["requires_hermes"] = floor
                path.write_text(yaml.safe_dump(manifest))
                with self.assertRaisesRegex(ValueError, "requires_hermes"):
                    check(self.root, release=True)

    def test_mismatched_version_and_symlink_are_rejected(self):
        path = self.root / "dashboard/manifest.json"
        original = path.read_text()
        manifest = json.loads(original)
        manifest["version"] = "999.0.0"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "disagree"):
            check(self.root)
        path.unlink()
        try:
            path.symlink_to(ROOT / "dashboard/manifest.json")
        except OSError:
            self.skipTest("Symlink creation is not available for this user")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            check(self.root)

    def test_reproducible_archive_excludes_private_and_test_files(self):
        self.allow_release()
        for name in ("HANDOVER.md", ".env", "plugin-private.json", "tests/private.txt"):
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("DO NOT SHIP")
        first, second = Path(self.temp.name) / "one", Path(self.temp.name) / "two"
        archive = build(self.root, first, "a" * 40)
        other = build(self.root, second, "a" * 40)
        self.assertEqual(archive.read_bytes(), other.read_bytes())
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(bundle.namelist()), {f"lan-control/{name}" for name in PACKAGE_FILES})
        inventory = json.loads((first / "package-inventory.json").read_text())
        self.assertEqual(inventory["commit"], "a" * 40)
        for name, digest in inventory["files"].items():
            self.assertEqual(digest, hashlib.sha256((self.root / name).read_bytes()).hexdigest())
        for line in (first / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((first / name).read_bytes()).hexdigest())

    def test_mutable_ref_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "40-character"):
            build(self.root, Path(self.temp.name) / "out", "main")


if __name__ == "__main__":
    unittest.main()
