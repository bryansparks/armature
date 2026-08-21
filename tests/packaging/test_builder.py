# tests/packaging/test_builder.py
from pathlib import Path
from ruamel.yaml import YAML
from armature.packaging.builder import PackageBuilder

_Y = YAML()

def test_build_assembles_full_tree(tmp_path, tiny_spec):
    out = tmp_path / "echo.pkg"
    pkg = PackageBuilder().build(spec=tiny_spec, out=out, inputs={"topic": "hello"})
    assert pkg == out
    for f in ["package.yaml", "workflow.yaml", "inputs.yaml", "secrets.yaml",
              "destinations.yaml", "requirements.txt", "manifest.sha256", "README.md"]:
        assert (pkg / f).exists(), f"missing {f}"

def test_build_auto_generates_secrets(tmp_path, tiny_spec):
    pkg = PackageBuilder().build(spec=tiny_spec, out=tmp_path / "echo.pkg", inputs={"topic": "x"})
    sf = _Y.load(pkg / "secrets.yaml")
    names = {r["name"] for r in sf["required"]}
    assert "OPENROUTER_API_KEY" in names

def test_build_default_destinations_infers_leaves(tmp_path, tiny_spec):
    pkg = PackageBuilder().build(spec=tiny_spec, out=tmp_path / "echo.pkg", inputs={"topic": "x"})
    dest = _Y.load(pkg / "destinations.yaml")
    stage_ids = {a["stage_id"] for a in dest["artifacts"]}
    assert "writer" in stage_ids  # writer is the leaf

def test_build_aborts_on_invalid_spec(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nversion: \"1.0\"\nstages: []\n")
    try:
        PackageBuilder().build(spec=bad, out=tmp_path / "bad.pkg", inputs={})
    except Exception:
        return
    raise AssertionError("expected build to abort on invalid spec")