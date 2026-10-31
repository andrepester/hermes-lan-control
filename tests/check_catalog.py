"""Offline catalog-update boundaries; never contact GitHub or create a commit."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_catalog_update import prepare


class CatalogChecks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = self.root / "plugin.yaml"
        self.backend = self.root / "manifest.json"
        self.entry = self.root / "upstream/plugin-catalog/lan-control.yaml"
        self.entry.parent.mkdir(parents=True)
        self.output = self.root / "preview"
        self.manifest.write_text(yaml.safe_dump({"name": "lan-control", "version": "0.2.4",
                                                "requires_hermes": ">=999.0.0"}))
        self.backend.write_text(json.dumps({"name": "lan-control", "version": "0.2.4"}))
        self.original = (
            "# Keep reviewed metadata and comments.\n"
            "name: lan-control\n"
            "repo: https://github.com/andrepester/hermes-lan-control\n"
            f'sha: "{"1" * 40}" # reviewed pin\n'
            'version: "0.2.3" # display label\n'
            'requires_hermes: ">=999.0.0"\n'
            "capabilities:\n  provides_tools: []\n"
            "screenshots: []\n"
        )
        self.entry.write_text(self.original)

    def run_prepare(self, commit="a" * 40, tag="v0.2.4"):
        return prepare(self.manifest, self.backend, self.entry, self.output, commit, tag)

    def test_patch_applies_and_changes_only_sha_and_version(self):
        metadata = self.run_prepare()
        self.assertTrue(metadata["changed"])
        self.assertEqual(self.entry.read_text(), self.original)
        subprocess.run(["git", "apply", "--check", str(self.output / "catalog-update.patch")],
                       cwd=self.entry.parents[1], check=True, capture_output=True)
        subprocess.run(["git", "apply", str(self.output / "catalog-update.patch")],
                       cwd=self.entry.parents[1], check=True, capture_output=True)
        actual = self.entry.read_text()
        self.assertEqual(actual, (self.output / "catalog-entry.yaml").read_text())
        expected = yaml.safe_load(self.original)
        expected.update(sha="a" * 40, version="0.2.4")
        self.assertEqual(yaml.safe_load(actual), expected)
        self.assertIn("# reviewed pin", actual)
        self.assertIn("# display label", actual)
        self.assertIn("# Keep reviewed metadata and comments.", actual)
        self.assertIn("compare/" + "1" * 40 + "..." + "a" * 40,
                      (self.output / "pull-request.md").read_text())
        self.assertEqual(len(list(self.output.iterdir())), 5)

    def test_missing_entry_requires_separate_initial_admission(self):
        self.entry.unlink()
        with self.assertRaisesRegex(ValueError, "initial admission"):
            self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_mutable_ref_and_mismatched_release_metadata_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "40-character"):
            self.run_prepare(commit="main")
        with self.assertRaisesRegex(ValueError, "tag"):
            self.run_prepare(tag="v0.2.5")
        self.backend.write_text(json.dumps({"name": "lan-control", "version": "0.2.5"}))
        with self.assertRaisesRegex(ValueError, "agree"):
            self.run_prepare()

    def test_wrong_repo_floor_change_and_duplicate_keys_fail_closed(self):
        cases = (
            (self.original.replace("andrepester/hermes-lan-control", "someone/another-plugin"), "this root-level"),
            (self.original.replace(">=999.0.0", ">=998.0.0"), "floor changed"),
            (self.original + 'sha: "' + "b" * 40 + '"\n', "Duplicate YAML key"),
        )
        for content, error in cases:
            with self.subTest(error=error):
                self.entry.write_text(content)
                with self.assertRaisesRegex(ValueError, error):
                    self.run_prepare()
                self.assertFalse(self.output.exists())

    def test_same_pin_and_version_is_a_noop(self):
        for content in (self.original, self.original.replace('sha: "' + "1" * 40 + '"', 'sha: "' + "a" * 40 + '"')):
            with self.subTest(content=content):
                content = content.replace("0.2.3", "0.2.4")
                commit = yaml.safe_load(content)["sha"]
                # A plain hex pin should not produce a quoting-only change.
                if commit == "a" * 40:
                    content = content.replace('"' + commit + '"', commit)
                self.entry.write_text(content)
                metadata = self.run_prepare(commit=commit)
                self.assertFalse(metadata["changed"])
                self.assertEqual((self.output / "catalog-update.patch").read_text(), "")
                self.assertEqual((self.output / "catalog-entry.yaml").read_text(), content)

    def test_fork_and_pr_job_stays_hard_disabled(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/catalog-update.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["jobs"]["submit"]["if"],
                         "${{ false && needs.prepare.outputs.changed == 'true' }}")
        for step in workflow["jobs"]["prepare"]["steps"]:
            self.assertNotIn("CATALOG_PR_TOKEN", json.dumps(step))


if __name__ == "__main__":
    unittest.main()
