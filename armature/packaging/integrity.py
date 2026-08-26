# armature/packaging/integrity.py
from __future__ import annotations
import hashlib
from pathlib import Path

_MANIFEST = "manifest.sha256"


def _iter_files(pkg_dir: Path):
    for p in sorted(pkg_dir.rglob("*")):
        if p.is_file() and p.name != _MANIFEST:
            yield p


def write_manifest_sha256(pkg_dir: Path) -> Path:
    """Write a sha256sum-format manifest over every file in the package."""
    lines = []
    for f in _iter_files(pkg_dir):
        rel = f.relative_to(pkg_dir).as_posix()
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{digest}  {rel}")
    out = pkg_dir / _MANIFEST
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out


def verify_integrity(pkg_dir: Path) -> bool:
    """Return True iff every file matches manifest.sha256 and no files are missing/extra."""
    mf = pkg_dir / _MANIFEST
    if not mf.exists():
        return False
    expected: dict[str, str] = {}
    for line in mf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, rel = line.partition("  ")
        if not digest or not rel:
            return False
        expected[rel] = digest
    actual = {}
    for f in _iter_files(pkg_dir):
        rel = f.relative_to(pkg_dir).as_posix()
        if rel not in expected:
            return False
        if hashlib.sha256(f.read_bytes()).hexdigest() != expected[rel]:
            return False
        actual[rel] = expected[rel]
    return actual == expected