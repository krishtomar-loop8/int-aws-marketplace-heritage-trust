"""Build and verify the release bundle manifest.

Stdlib-only and Python 3.9+ compatible: it runs on the CodeBuild image's python3.

Usage:
    python -m installer.bundle_manifest build <root> <version> <commit>
    python -m installer.bundle_manifest verify <root>
"""

import hashlib
import json
import sys
from pathlib import Path

MANIFEST_NAME = "manifest.json"


def _hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan(root):
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): _hash(p)
        for p in sorted(root.rglob("*"), key=lambda q: q.relative_to(root).as_posix())
        if p.is_file()
        and p.relative_to(root).as_posix() != MANIFEST_NAME
        and "__pycache__" not in p.relative_to(root).parts  # written by verify's own import
    }


def build_manifest(root, version, commit):
    return {"version": version, "commit": commit, "files": _scan(root)}


def verify_manifest(root):
    root = Path(root)
    expected = json.loads((root / MANIFEST_NAME).read_text())["files"]
    actual = _scan(root)
    bad = {p for p in expected if actual.get(p) != expected[p]}
    bad |= set(actual) - set(expected)
    return sorted(bad)


def main(argv):
    if len(argv) == 5 and argv[1] == "build":
        root = Path(argv[2])
        manifest = build_manifest(root, argv[3], argv[4])
        (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return 0
    if len(argv) == 3 and argv[1] == "verify":
        try:
            bad = verify_manifest(argv[2])
        except (OSError, ValueError, KeyError) as exc:
            print(f"cannot read manifest: {exc}", file=sys.stderr)
            return 1
        if bad:
            print("Bundle verification failed (missing, changed or extra):")
            for path in bad:
                print(f"  {path}")
            return 1
        print("Bundle verified.")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
