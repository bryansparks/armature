# tests/packaging/test_builder.py
from pathlib import Path
from ruamel.yaml import YAML
from armature.packaging.builder import PackageBuilder, PackageBuildError

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
    bad.write_text('name: bad\nversion: "1.0"\n')
    try:
        PackageBuilder().build(spec=bad, out=tmp_path / "bad.pkg", inputs={})
    except Exception:
        return
    raise AssertionError("expected build to abort on invalid spec")


LAYERED_SPEC = """\
name: layered
version: "1.0"
description: Layer-bundling spec for package tests.
model_tiers:
  small:
    provider: openrouter
    model: qwen/qwen3.6-27b
    api_key_env: OPENROUTER_API_KEY
contracts:
  inputs:
    - name: topic
context_layers:
  - name: principles
    src: principles.md
stages:
  - id: writer
    role: {name: Writer, type: worker, description: "Echo {{ topic }}"}
    output_mode: text
    depends_on: []
"""


def test_build_bundles_context_layer_src_files(tmp_path):
    (tmp_path / "principles.md").write_text("Be terse.", encoding="utf-8")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(LAYERED_SPEC)
    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg",
                                 inputs={"topic": "x"})
    assert (pkg / "principles.md").read_text(encoding="utf-8") == "Be terse."
    assert "principles.md" in (pkg / "manifest.sha256").read_text(encoding="utf-8")


def test_build_rejects_layer_src_escaping_package_dir(tmp_path):
    (tmp_path / "secret.md").write_text("outside", encoding="utf-8")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(LAYERED_SPEC.replace("src: principles.md", "src: ../secret.md"))
    try:
        PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg",
                               inputs={"topic": "x"})
    except PackageBuildError:
        return
    raise AssertionError("expected build to abort on escaping layer src")