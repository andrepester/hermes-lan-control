"""Prepare a catalog SHA-bump patch and PR text; never use network or Git writes."""
import argparse
import difflib
import json
from pathlib import Path
import re

import yaml


ENTRY = "plugin-catalog/lan-control.yaml"
REPO = "https://github.com/andrepester/hermes-lan-control"
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


class UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous YAML mappings rather than silently taking the last key."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_yaml(text):
    result = yaml.load(text, Loader=UniqueLoader)
    if not isinstance(result, dict):
        raise ValueError("Expected a YAML mapping")
    return result


def prepare(manifest_path, backend_path, entry_path, output, commit, tag):
    if not SHA.fullmatch(commit):
        raise ValueError("Use an exact 40-character commit SHA")
    manifest = load_yaml(Path(manifest_path).read_text(encoding="utf-8"))
    backend = json.loads(Path(backend_path).read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION.fullmatch(version) or tag != f"v{version}":
        raise ValueError("Release tag must match the native manifest version")
    if manifest.get("name") != "lan-control" or backend.get("name") != "lan-control" or backend.get("version") != version:
        raise ValueError("Native and backend package identities/versions must agree")
    floor = manifest.get("requires_hermes")
    if not isinstance(floor, str) or not floor.strip():
        raise ValueError("Verify and declare requires_hermes before catalog updates")
    entry_path = Path(entry_path)
    if not entry_path.is_file():
        raise ValueError("No existing catalog entry: initial admission needs a separate reviewed submission")
    original = entry_path.read_text(encoding="utf-8")
    if not original.endswith("\n"):
        raise ValueError("Catalog entry must end with a newline for a reviewable Git patch")
    entry = load_yaml(original)
    if entry.get("name") != "lan-control" or entry.get("repo") != REPO or entry.get("subdir", "") != "":
        raise ValueError("Catalog entry must refer to this root-level native package")
    old_sha = entry.get("sha")
    if not isinstance(old_sha, str) or not SHA.fullmatch(old_sha):
        raise ValueError("Existing catalog pin must be a full commit SHA")
    if entry.get("requires_hermes") != floor:
        raise ValueError("Compatibility floor changed: review catalog metadata separately")
    updated = original
    for key, value in (("sha", commit), ("version", version)):
        # Preserve comments and all other catalog fields; reject exotic layouts.
        pattern = re.compile(rf"^({key}:)[ \t]*(?:\"[^\"\n]*\"|'[^'\n]*'|[^#\n]*?)([ \t]+#[^\n]*)?$", re.MULTILINE)
        updated, count = pattern.subn(lambda match: f"{match[1]} {json.dumps(value)}{match[2] or ''}", updated)
        if count != 1:
            raise ValueError(f"Expected exactly one simple top-level {key} field")
    parsed = load_yaml(updated)
    if parsed != {**entry, "sha": commit, "version": version}:
        raise ValueError("Refusing changes beyond the catalog SHA and version")
    changed = old_sha != commit or entry.get("version") != version
    if not changed:
        updated = original
    title = f"Update LAN Control to {version}"
    metadata = {"changed": changed, "old_sha": old_sha, "commit": commit,
                "version": version, "tag": tag, "entry": ENTRY, "title": title,
                "branch": f"chore/lan-control-{version}-{commit[:12]}"}
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True),
                                       updated.splitlines(keepends=True),
                                       fromfile=f"a/{ENTRY}", tofile=f"b/{ENTRY}"))
    body = f"""Update the existing LAN Control catalog pin to version {version}.

Release: {REPO}/releases/tag/{tag}
Source diff: {REPO}/compare/{old_sha}...{commit}
Exact new commit: `{commit}`
Hermes compatibility requirement: `{floor}` (unchanged).

Only the catalog SHA and version label change. Existing capabilities, platform
claims, screenshots, and other metadata are retained and require human review
against the new source. This automation does not certify that they remain valid.

## Validation and review

- [ ] CI and security checks passed for the exact new commit.
- [ ] `hermes plugins validate <checkout> --install-deps` passed in isolation.
- [ ] Native lifecycle checks and compatibility evidence were reviewed.
- [ ] Declared capabilities and README behavior disclosures match the new code.
- [ ] The old-to-new source diff and scanner warnings were reviewed.

Prepared by the plugin owner's release automation. Admission and SHA updates
remain subject to Hermes maintainer review; no automatic merge is requested.
"""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in (("catalog-update.patch", patch), ("catalog-entry.yaml", updated),
                          ("pull-request.md", body), ("pull-request-title.txt", title + "\n"),
                          ("update.json", json.dumps(metadata, indent=2) + "\n")):
        (output / name).write_text(content, encoding="utf-8")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backend-manifest", type=Path, required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    result = prepare(args.manifest, args.backend_manifest, args.entry, args.output, args.commit, args.tag)
    print(json.dumps(result))
