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

# ── subagent_spec bundling (Fargate blocker: children must ship in the pkg) ──

import logging as _logging

from armature.packaging.integrity import verify_integrity

SUBAGENT_PARENT = """\
name: subagent-demo
version: "1.0"
description: Parent workflow with a subagent stage.
contracts:
  inputs: []
stages:
  - id: spawn
    subagent_spec: workflows/child.yaml
    depends_on: []
"""

CHILD_NO_LLM = """\
name: child
version: "1.0"
description: No-LLM child (script adapter) for packaging tests.
adapters:
  greet:
    name: greet
    type: script
    cmd: "echo 'child says: {{greeting}}'"
stages:
  - id: respond
    adapter: greet
"""


def _write_child(dir_: Path, text: str = CHILD_NO_LLM, name: str = "child.yaml") -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_text(text, encoding="utf-8")
    return p


def test_build_bundles_subagent_spec_files(tmp_path):
    _write_child(tmp_path / "workflows")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT)
    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    assert (pkg / "workflows" / "child.yaml").read_text(encoding="utf-8") == CHILD_NO_LLM
    assert "workflows/child.yaml" in (pkg / "manifest.sha256").read_text(encoding="utf-8")
    assert verify_integrity(pkg)


def test_build_bundles_nested_subagent_specs(tmp_path):
    grandchild = _write_child(tmp_path / "workflows" / "inner", name="grand.yaml")
    child = CHILD_NO_LLM.replace(
        "stages:\n  - id: respond\n    adapter: greet\n",
        "stages:\n  - id: respond\n    adapter: greet\n  - id: spawn_inner\n"
        "    subagent_spec: inner/grand.yaml\n",
    )
    _write_child(tmp_path / "workflows", text=child)
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT)
    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    assert (pkg / "workflows" / "child.yaml").exists()
    assert (pkg / "workflows" / "inner" / "grand.yaml").exists()
    assert verify_integrity(pkg)


def test_build_bundles_child_context_layer_srcs(tmp_path):
    child = CHILD_NO_LLM.replace(
        "stages:\n",
        "context_layers:\n  - name: notes\n    src: notes.md\nstages:\n",
    )
    _write_child(tmp_path / "workflows", text=child)
    (tmp_path / "workflows" / "notes.md").write_text("child-level layer", encoding="utf-8")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT)
    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    # Vendored next to the bundled child, where the child's own load resolves it.
    assert (pkg / "workflows" / "notes.md").read_text(encoding="utf-8") == "child-level layer"
    assert verify_integrity(pkg)


def test_build_rejects_missing_subagent_spec(tmp_path):
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT)  # workflows/child.yaml never created
    try:
        PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    except PackageBuildError as exc:
        assert "workflows/child.yaml" in str(exc)
        return
    raise AssertionError("expected build to abort on missing subagent spec")


def test_build_rejects_absolute_subagent_spec_ref(tmp_path):
    child = _write_child(tmp_path / "elsewhere", name="child.yaml")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT.replace(
        "subagent_spec: workflows/child.yaml", f"subagent_spec: {child}"))
    try:
        PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    except PackageBuildError as exc:
        assert "absolute" in str(exc)
        return
    raise AssertionError("expected build to abort on absolute subagent_spec")


def test_build_rejects_subagent_ref_escaping_package_dir(tmp_path):
    _write_child(tmp_path)  # at repo root, one level above the spec's dir
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    spec_path = spec_dir / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT.replace(
        "subagent_spec: workflows/child.yaml", "subagent_spec: ../child.yaml"))
    try:
        PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    except PackageBuildError as exc:
        assert "escapes" in str(exc)
        return
    raise AssertionError("expected build to abort on escaping subagent ref")


def test_build_handles_subagent_reference_cycles(tmp_path):
    # Child references its own parent: the walk must terminate and the
    # package must still be complete.
    child = CHILD_NO_LLM.replace(
        "stages:\n",
        "stages:\n  - id: respawn\n    subagent_spec: ../workflow.yaml\n",
    )
    _write_child(tmp_path / "workflows", text=child)
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT)
    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    assert (pkg / "workflows" / "child.yaml").exists()
    assert verify_integrity(pkg)


def test_build_warns_and_skips_templated_subagent_ref(tmp_path, caplog):
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(SUBAGENT_PARENT.replace(
        "subagent_spec: workflows/child.yaml", "subagent_spec: '{{ child_ref }}'"))
    with caplog.at_level(_logging.WARNING, logger="armature.packaging.builder"):
        pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg", inputs={})
    assert any("templated" in r.message for r in caplog.records)
    assert pkg.exists()


def test_build_carries_spec_destinations_over_inference(tmp_path):
    """A spec's destinations section (artifacts with file-capture sources)
    must reach destinations.yaml verbatim — inference would drop the
    source field and the run would capture nothing."""
    spec_file = tmp_path / "workflow.yaml"
    spec_file.write_text(
        "name: filecap\n"
        "destinations:\n"
        "  artifacts:\n"
        "    - stage_id: writer\n"
        "      name: report\n"
        "      format: text\n"
        "      source: research-output/report.html\n"
        "  include_trace: true\n"
        "adapters:\n"
        "  wt: {name: wt, type: script, cmd: \"mkdir -p research-output && echo x > research-output/report.html\"}\n"
        "stages:\n"
        "  - id: writer\n"
        "    adapter: wt\n",
        encoding="utf-8",
    )
    out = PackageBuilder().build(spec=spec_file, out=tmp_path / "pkg", inputs={})
    from ruamel.yaml import YAML
    dest = YAML().load((out / "destinations.yaml").read_text())
    assert dest["include_trace"] is True
    assert len(dest["artifacts"]) == 1
    a = dest["artifacts"][0]
    assert a["source"] == "research-output/report.html"
    assert a["stage_id"] == "writer"


def test_build_explicit_destinations_file_beats_spec_section(tmp_path):
    """Precedence: --destinations file > spec destinations > inferred. An
    explicit operator-provided file is authoritative."""
    spec_file = tmp_path / "workflow.yaml"
    spec_file.write_text(
        "name: filecap\n"
        "destinations:\n"
        "  artifacts:\n"
        "    - {stage_id: writer, name: from_spec, format: text}\n"
        "stages:\n"
        "  - id: writer\n"
        "    role: {name: W, type: worker, description: hi}\n",
        encoding="utf-8",
    )
    explicit = tmp_path / "dest.yaml"
    explicit.write_text(
        "artifacts:\n"
        "  - {stage_id: writer, name: from_file, format: markdown}\n"
        "include_trace: false\n",
        encoding="utf-8",
    )
    out = PackageBuilder().build(spec=spec_file, out=tmp_path / "pkg", inputs={},
                                  destinations=explicit)
    from ruamel.yaml import YAML
    dest = YAML().load((out / "destinations.yaml").read_text())
    assert [a["name"] for a in dest["artifacts"]] == ["from_file"]


def test_build_secrets_yaml_includes_tool_envs(tmp_path):
    """Tool-level api_key_env declarations flow into the generated
    secrets.yaml so fail-closed runners inject them."""
    tools_src = tmp_path / "tools_src"
    (tools_src / "research" / "tools").mkdir(parents=True)
    (tools_src / "research" / "tools" / "web.py").write_text(
        "def register(registry):\n    pass\n", encoding="utf-8")
    spec_file = tmp_path / "workflow.yaml"
    spec_file.write_text(
        "name: wf\n"
        "tools:\n"
        "  - module: research.tools.web\n"
        "    api_key_env: [TAVILY_API_KEY, GITHUB_TOKEN]\n"
        "stages:\n"
        "  - id: s1\n"
        "    role: {name: W, type: worker, description: hi}\n",
        encoding="utf-8",
    )
    out = PackageBuilder().build(spec=spec_file, out=tmp_path / "pkg", inputs={},
                                  tools=tools_src)
    from ruamel.yaml import YAML
    sf = YAML().load((out / "secrets.yaml").read_text())
    names = {r["name"] for r in sf["required"]}
    assert {"TAVILY_API_KEY", "GITHUB_TOKEN"} <= names
