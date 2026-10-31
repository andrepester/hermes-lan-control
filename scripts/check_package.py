"""Validate this repository's native package without importing plugin code."""
import argparse
import json
from pathlib import Path
import re

import yaml


PACKAGE_FILES = (
    "LICENSE", "README.md", "RUNBOOK.md", "SECURITY.md", "__init__.py",
    "plugin.yaml", "desktop/plugin.js", "dashboard/manifest.json",
    "dashboard/plugin_api.py",
    "docs/screenshots/status-green.png", "docs/screenshots/status-yellow.png",
    "docs/screenshots/status-red.png",
    "docs/screenshots/statusbar-context.png",
    "docs/screenshots/settings.png",
    "docs/compatibility/hermes-0.22.5.md",
    "docs/compatibility/hermes-0.22.5-native.json",
    "docs/compatibility/older-hermes.md",
    "docs/compatibility/older-hermes.json",
    "docs/compatibility/hermes-0.22.3-native.json",
)


def check(root, release=False):
    root = Path(root).resolve()
    for name in PACKAGE_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or unsafe package file: {name}")
    if (root / "plugin.json").exists():
        raise ValueError("This native plugin must not use portable plugin.json packaging")
    manifest = yaml.safe_load((root / "plugin.yaml").read_text())
    backend = json.loads((root / "dashboard/manifest.json").read_text())
    if manifest["name"] != "lan-control" or backend["name"] != "lan-control":
        raise ValueError("Keep the lan-control identity stable")
    version = manifest["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("Expected a semantic package version")
    if version != backend["version"] or backend["api"] != "plugin_api.py":
        raise ValueError("Backend and native manifest disagree")
    if manifest.get("manifest_version") != 2 or manifest.get("api_version") != 1:
        raise ValueError("Unexpected native manifest/API version")
    if manifest.get("license") != "MIT" or manifest.get("homepage") != "https://github.com/andrepester/hermes-lan-control":
        raise ValueError("Missing package license or canonical homepage")
    secret = manifest["config_schema"]["bearer_token"]
    if secret.get("type") != "secret" or secret.get("env") != "LAN_CONTROL_BEARER_TOKEN":
        raise ValueError("Bearer token must use Hermes secret storage")
    floor = manifest.get("requires_hermes")
    if release and (not isinstance(floor, str) or not floor.strip()):
        raise ValueError("Release blocked: verify and declare requires_hermes first")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    print(f"Native package OK: {check(args.root, args.release)}")
