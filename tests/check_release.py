"""Release boundary checks; no plugin imports or network traffic; disposable Git fixtures only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
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


class ReleaseWorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {key: os.environ[key] for key in
                            ("PATH", "SYSTEMROOT", "SystemRoot", "TMPDIR", "TMP", "TEMP")
                            if key in os.environ}
        self.environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "old reviewed tree")
        self.older = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.2.2")
        self.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "dispatch snapshot")
        self.commit = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.2.3")
        self.git("update-ref", "refs/remotes/origin/main", self.commit)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              env=self.environment, text=True, capture_output=True,
                              timeout=15).stdout.strip()

    def preflight(self, **overrides):
        output = self.root / "outputs"
        output.unlink(missing_ok=True)
        environment = {**self.environment, "GITHUB_REF": "refs/heads/main",
                       "GITHUB_SHA": self.commit, "GITHUB_OUTPUT": str(output),
                       "VALIDATED": "true", "RELEASE_COMMIT": self.commit,
                       "RELEASE_TAG": "v0.2.3", **overrides}
        steps = self.workflow["jobs"]["preflight"]["steps"]
        script = next(step["run"] for step in steps if step.get("id") == "verify")
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                cwd=self.root, env=environment, text=True,
                                capture_output=True, timeout=15)
        return result.returncode, output.read_text() if output.exists() else ""

    def test_preflight_accepts_only_approved_tagged_dispatch_snapshot(self):
        self.assertEqual(self.preflight(), (0, f"commit={self.commit}\ntag=v0.2.3\n"))
        cases = (
            {"VALIDATED": "false"},
            {"GITHUB_REF": "refs/heads/review-branch"},
            {"GITHUB_REF": "refs/tags/v0.2.3"},
            {"RELEASE_COMMIT": self.older, "RELEASE_TAG": "v0.2.2"},
            {"RELEASE_TAG": "v0.2.2"},
            {"RELEASE_COMMIT": "main"},
            {"RELEASE_TAG": "v0.2.3; touch injected"},
            {"RELEASE_COMMIT": "$(touch injected)"},
            {"GITHUB_SHA": self.older, "RELEASE_COMMIT": self.older,
             "RELEASE_TAG": "v0.2.2"},
        )
        for values in cases:
            with self.subTest(values=values):
                code, output = self.preflight(**values)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                self.assertFalse((self.root / "injected").exists())

        self.git("update-ref", "refs/remotes/origin/main", self.older)
        self.assertNotEqual(self.preflight()[0], 0)
        self.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "main advances")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        self.git("checkout", "--detach", self.commit)
        self.assertEqual(self.preflight(), (0, f"commit={self.commit}\ntag=v0.2.3\n"))

    def test_release_jobs_keep_execution_and_write_permissions_separate(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(jobs["preflight"]["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for name in ("ci", "security"):
            self.assertEqual(jobs[name]["needs"], "preflight")
            self.assertEqual(jobs[name]["with"]["ref"], "${{ github.sha }}")
        package = jobs["package"]
        self.assertEqual(set(package["needs"]), {"preflight", "ci", "security"})
        checkout = next(step for step in package["steps"]
                        if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertNotIn("permissions", package)
        draft = jobs["draft"]
        self.assertEqual(draft["permissions"], {"contents": "write"})
        self.assertEqual(set(draft["needs"]), {"preflight", "package"})
        self.assertFalse(any("checkout@" in step.get("uses", "") for step in draft["steps"]))
        command = next(step["run"] for step in draft["steps"] if "run" in step)
        self.assertIn("--draft", command)
        self.assertIn("--verify-tag", command)


if __name__ == "__main__":
    unittest.main()
