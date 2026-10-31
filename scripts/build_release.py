"""Build a deterministic, allowlisted native package and SHA-256 inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from check_package import PACKAGE_FILES, check


def build(root, output, commit):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("An exact 40-character commit SHA is required")
    version = check(root, release=True)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"lan-control-{version}.zip"
    inventory = {"name": "lan-control", "version": version, "commit": commit, "files": {}}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in sorted(PACKAGE_FILES):
            content = (root / name).read_bytes()
            inventory["files"][name] = hashlib.sha256(content).hexdigest()
            entry = zipfile.ZipInfo(f"lan-control/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, content, compresslevel=9)
    provenance = output / "package-inventory.json"
    provenance.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                        for path in (archive, provenance))
    (output / "SHA256SUMS").write_text(checksums)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(build(args.root, args.output, args.commit))
