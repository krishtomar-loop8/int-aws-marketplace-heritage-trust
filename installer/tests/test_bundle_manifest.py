import json

from installer.bundle_manifest import build_manifest, verify_manifest


def _tree(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    return tmp_path


def _write(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_roundtrip_ok(tmp_path):
    root = _tree(tmp_path)
    _write(root, build_manifest(root, "1.0.0", "abc"))
    assert verify_manifest(root) == []


def test_detects_changed_file(tmp_path):
    root = _tree(tmp_path)
    _write(root, build_manifest(root, "1.0.0", "abc"))
    (root / "a.txt").write_text("tampered")
    assert verify_manifest(root) == ["a.txt"]


def test_detects_missing_file(tmp_path):
    root = _tree(tmp_path)
    _write(root, build_manifest(root, "1.0.0", "abc"))
    (root / "sub" / "b.txt").unlink()
    assert verify_manifest(root) == ["sub/b.txt"]


def test_detects_extra_file(tmp_path):
    root = _tree(tmp_path)
    _write(root, build_manifest(root, "1.0.0", "abc"))
    (root / "evil.sh").write_text("x")
    assert verify_manifest(root) == ["evil.sh"]


def test_manifest_excludes_itself(tmp_path):
    root = _tree(tmp_path)
    _write(root, build_manifest(root, "1.0.0", "abc"))
    m = build_manifest(root, "1.0.0", "abc")
    assert "manifest.json" not in m["files"]
    assert m["version"] == "1.0.0" and m["commit"] == "abc"
    assert list(m["files"]) == ["a.txt", "sub/b.txt"]


def test_ignores_pycache(tmp_path):
    root = _tree(tmp_path)
    _write(root, build_manifest(root, "1.0.0", "abc"))
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert verify_manifest(root) == []
