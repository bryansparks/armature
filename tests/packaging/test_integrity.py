# tests/packaging/test_integrity.py
from pathlib import Path
from armature.packaging.integrity import write_manifest_sha256, verify_integrity

def test_write_then_verify(tmp_path: Path):
    pkg = tmp_path / "demo.pkg"
    pkg.mkdir()
    (pkg / "workflow.yaml").write_text("name: demo\n")
    (pkg / "inputs.yaml").write_text("topic: x\n")
    out = write_manifest_sha256(pkg)
    assert out == pkg / "manifest.sha256"
    assert verify_integrity(pkg) is True

def test_tamper_fails(tmp_path: Path):
    pkg = tmp_path / "demo.pkg"
    pkg.mkdir()
    (pkg / "workflow.yaml").write_text("name: demo\n")
    write_manifest_sha256(pkg)
    (pkg / "workflow.yaml").write_text("name: CHANGED\n")
    assert verify_integrity(pkg) is False

def test_missing_manifest_fails(tmp_path: Path):
    pkg = tmp_path / "demo.pkg"
    pkg.mkdir()
    (pkg / "workflow.yaml").write_text("name: demo\n")
    assert verify_integrity(pkg) is False

def test_extra_file_fails(tmp_path: Path):
    pkg = tmp_path / "demo.pkg"
    pkg.mkdir()
    (pkg / "workflow.yaml").write_text("name: demo\n")
    write_manifest_sha256(pkg)
    (pkg / "sneaky.txt").write_text("new file not in manifest\n")
    assert verify_integrity(pkg) is False