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
    """armature/__init__ (and everything it pulls) must not reach transport.

    Runs in a fresh interpreter: this directory's other test modules import
    armature.transport at collection time, so an in-process check would see
    their imports, not core's."""
    import os
    import subprocess
    import sys
    code = ("import sys, armature; "
            "ok = not any(m.startswith('armature.transport') for m in sys.modules) "
            "and 'boto3' not in sys.modules; "
            "sys.exit(0 if ok else 1)")
    env = dict(os.environ, PYTHONPATH=str(REPO))
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        "import armature reached transport or boto3:\n" + result.stderr)


def test_transport_package_and_extras_exist():
    """The subpackage and its extras wiring exist (and can never silently vanish)."""
    assert (PKG / "transport" / "__init__.py").is_file(), \
        "armature/transport/ package is missing"
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'cloud = [' in pyproject, "cloud extra not declared in pyproject.toml"
    assert 'cloud-dev = [' in pyproject, "cloud-dev extra not declared in pyproject.toml"

def _imports(py: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_the_provisioner_seam_imports_no_aws_sdk():
    banned = [n for n in _imports(PKG / "transport" / "provision.py") if n.split(".")[0] in BANNED]
    assert not banned, f"provision.py imports {banned}"


def test_the_server_reaches_ec2_only_through_the_provider_loader():
    offenders = []
    for py in (PKG / "server").rglob("*.py"):
        if any(n == "armature.transport.ec2" for n in _imports(py)):
            offenders.append(str(py.relative_to(REPO)))
        elif "armature.transport.ec2" in py.read_text(encoding="utf-8") and py.name != "provider_loader.py":
            offenders.append(str(py.relative_to(REPO)))
    assert not offenders, "the server names armature.transport.ec2 outside provider_loader: " + ", ".join(offenders)


def test_starting_the_server_app_never_imports_boto3():
    import os
    import subprocess
    code = ("import sys, armature.server.app, armature.server.environments; "
            "sys.exit(1 if 'boto3' in sys.modules or 'armature.transport.ec2' in sys.modules else 0)")
    result = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONPATH=str(REPO)),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
