"""The extras boundary: core armature must never import AWS SDKs.

The `cloud` extra is the whole abstraction (spec §Decisions) — this test is
what keeps it true as the transport code lands next to the engine."""
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "armature"
BANNED = ("boto3", "botocore", "moto")


def test_no_aws_imports_outside_transport():
    offenders = []
    for py in PKG.rglob("*.py"):
        if "transport" in py.relative_to(PKG).parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in BANNED:
                    offenders.append(f"{py.relative_to(REPO)}: {name}")
    assert not offenders, "AWS imports outside armature/transport: " + ", ".join(offenders)


def test_transport_is_not_imported_by_core():
    """armature/__init__ (and everything it pulls) must not reach transport."""
    import armature  # noqa: F401
    assert not any(m.startswith("armature.transport") for m in sys.modules), \
        "importing armature imported armature.transport"


def test_transport_package_and_extras_exist():
    """The subpackage and its extras wiring exist (and can never silently vanish)."""
    assert (PKG / "transport" / "__init__.py").is_file(), \
        "armature/transport/ package is missing"
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'cloud = [' in pyproject, "cloud extra not declared in pyproject.toml"
    assert 'cloud-dev = [' in pyproject, "cloud-dev extra not declared in pyproject.toml"