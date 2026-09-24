# tests/packaging/test_verifier.py
from pathlib import Path
from ruamel.yaml import YAML
from armature.packaging.manifest import PackageManifest, SecretsFile, SecretRequirement, Destinations, ArtifactSpec
from armature.packaging.verifier import CompletenessVerifier, VerificationReport

_Y = YAML()

def _write(path: Path, obj):
    _Y.dump(obj, path)

def _make_pkg(tmp_path, tiny_spec, *, secrets=None, inputs=None, destinations=None,
              runtime_inputs=None, tools_dir=False):
    pkg = tmp_path / "echo.pkg"
    pkg.mkdir()
    (pkg / "workflow.yaml").write_text(tiny_spec.read_text())
    _write(pkg / "inputs.yaml", inputs if inputs is not None else {"topic": "hello"})
    _write(pkg / "secrets.yaml",
           (secrets if secrets is not None else
            {"required": [{"name": "OPENROUTER_API_KEY"}]}))
    _write(pkg / "destinations.yaml",
           (destinations if destinations is not None else
            {"artifacts": [{"stage_id": "writer", "name": "out", "format": "markdown"}],
             "include_trace": False}))
    (pkg / "requirements.txt").write_text("armature-agents\n")
    if tools_dir:
        (pkg / "tools").mkdir()
    manifest = PackageManifest(
        name="echo-demo", version="1.0", armature_version=">=0.6.0",
        created_at="2026-08-21T12:00:00Z",
        runtime_inputs=runtime_inputs or [],
        tools_dir="tools/" if tools_dir else None,
    )
    return pkg, manifest

def test_verifier_pass(tmp_path, tiny_spec):
    pkg, manifest = _make_pkg(tmp_path, tiny_spec)
    report = CompletenessVerifier().verify(pkg, manifest)
    assert report.ok, [c.detail for c in report.checks]
    assert {c.check for c in report.checks} >= {
        "SPEC_VALID", "INPUTS_COMPLETE", "SECRETS_DECLARED", "TOOLS_RESOLVABLE",
        "SANDBOX_IMAGE", "ARTIFACTS_VALID", "DEPS_RESOLVE", "INTEGRITY",
    }
    assert (pkg / "manifest.sha256").exists()  # V8 wrote it

def test_v2_missing_input_fails(tmp_path, tiny_spec):
    pkg, manifest = _make_pkg(tmp_path, tiny_spec, inputs={"not_topic": "x"})
    report = CompletenessVerifier().verify(pkg, manifest)
    v2 = next(c for c in report.checks if c.check == "INPUTS_COMPLETE")
    assert v2.status == "fail"

def test_v3_undeclared_secret_fails(tmp_path, tiny_spec):
    pkg, manifest = _make_pkg(tmp_path, tiny_spec, secrets={"required": []})
    report = CompletenessVerifier().verify(pkg, manifest)
    v3 = next(c for c in report.checks if c.check == "SECRETS_DECLARED")
    assert v3.status == "fail"

def test_v3_profile_resolves(tmp_path, tiny_spec, monkeypatch):
    pkg, manifest = _make_pkg(tmp_path, tiny_spec)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    report = CompletenessVerifier().verify(pkg, manifest, profile_env={"OPENROUTER_API_KEY": "sk-test"})
    v3 = next(c for c in report.checks if c.check == "SECRETS_DECLARED")
    assert v3.status == "pass"

def test_v6_dangling_artifact_fails(tmp_path, tiny_spec):
    pkg, manifest = _make_pkg(tmp_path, tiny_spec,
        destinations={"artifacts": [{"stage_id": "nope", "name": "x", "format": "markdown"}]})
    report = CompletenessVerifier().verify(pkg, manifest)
    v6 = next(c for c in report.checks if c.check == "ARTIFACTS_VALID")
    assert v6.status == "fail"

def test_v1_invalid_spec_fails(tmp_path):
    pkg = tmp_path / "bad.pkg"; pkg.mkdir()
    (pkg / "workflow.yaml").write_text("name: bad\nversion: \"1.0\"\n")  # missing required `stages` field
    _write(pkg / "inputs.yaml", {})
    _write(pkg / "secrets.yaml", {"required": []})
    _write(pkg / "destinations.yaml", {"artifacts": []})
    (pkg / "requirements.txt").write_text("armature-agents\n")
    manifest = PackageManifest(name="bad", version="1.0", armature_version=">=0.6.0",
                               created_at="2026-08-21T12:00:00Z")
    report = CompletenessVerifier().verify(pkg, manifest)
    v1 = next(c for c in report.checks if c.check == "SPEC_VALID")
    assert v1.status == "fail"

# ── V9: subagent specs must ship inside the package ────────────────────────────

from armature.packaging.builder import PackageBuilder as _Builder

_V9_PARENT = """\
name: subagent-demo
version: "1.0"
description: Parent workflow with a subagent stage.
contracts:
  inputs: []
stages:
  - id: spawn
    subagent_spec: workflows/child.yaml
"""

_V9_CHILD = """\
name: child
version: "1.0"
adapters:
  greet:
    name: greet
    type: script
    cmd: "echo hi"
stages:
  - id: respond
    adapter: greet
"""


def _v9_build(tmp_path):
    (tmp_path / "workflows").mkdir(exist_ok=True)
    (tmp_path / "workflows" / "child.yaml").write_text(_V9_CHILD, encoding="utf-8")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(_V9_PARENT, encoding="utf-8")
    pkg = _Builder().build(spec=spec_path, out=tmp_path / "pkg", inputs={})
    manifest = PackageManifest.model_validate(_Y.load(pkg / "package.yaml"))
    return pkg, manifest


def _v9(report):
    return next(c for c in report.checks if c.check == "SUBAGENTS_BUNDLED")


def test_v9_bundled_subagent_spec_passes(tmp_path):
    pkg, manifest = _v9_build(tmp_path)
    report = CompletenessVerifier().verify(pkg, manifest, write_integrity=False)
    v9 = _v9(report)
    assert v9.status == "pass"
    assert "1 subagent spec" in v9.detail


def test_v9_missing_bundled_subagent_spec_fails(tmp_path):
    pkg, manifest = _v9_build(tmp_path)
    (pkg / "workflows" / "child.yaml").unlink()
    report = CompletenessVerifier().verify(pkg, manifest, write_integrity=False)
    v9 = _v9(report)
    assert v9.status == "fail"
    assert "workflows/child.yaml" in v9.detail
    assert "not bundled" in v9.detail


def test_v9_checks_child_refs_recursively(tmp_path):
    (tmp_path / "workflows" / "inner").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workflows" / "inner" / "grand.yaml").write_text(_V9_CHILD, encoding="utf-8")
    child = _V9_CHILD.replace(
        "stages:\n",
        "stages:\n  - id: respawn\n    subagent_spec: inner/grand.yaml\n",
    )
    (tmp_path / "workflows" / "child.yaml").write_text(child, encoding="utf-8")
    spec_path = tmp_path / "workflow.yaml"
    spec_path.write_text(_V9_PARENT, encoding="utf-8")
    pkg = _Builder().build(spec=spec_path, out=tmp_path / "pkg", inputs={})
    manifest = PackageManifest.model_validate(_Y.load(pkg / "package.yaml"))

    (pkg / "workflows" / "inner" / "grand.yaml").unlink()
    report = CompletenessVerifier().verify(pkg, manifest, write_integrity=False)
    v9 = _v9(report)
    assert v9.status == "fail"
    assert "inner/grand.yaml" in v9.detail


def test_v9_flags_absolute_subagent_ref(tmp_path):
    # The builder rejects absolute refs, so this is the manual-assembly case:
    # a handcrafted package pointing at a spec outside itself.
    outside = tmp_path / "outside_child.yaml"
    outside.write_text(_V9_CHILD, encoding="utf-8")
    parent = _V9_PARENT.replace("workflows/child.yaml", str(outside))
    pkg, manifest = _make_pkg(tmp_path, type("S", (), {"read_text": lambda self: parent})())
    report = CompletenessVerifier().verify(pkg, manifest)
    v9 = _v9(report)
    assert v9.status == "fail"
    assert "not portable" in v9.detail


def test_v9_warns_on_templated_subagent_ref(tmp_path):
    parent = _V9_PARENT.replace(
        "subagent_spec: workflows/child.yaml", "subagent_spec: '{{ child_ref }}'")
    spec_stub = type("S", (), {"read_text": lambda self: parent})()
    pkg, manifest = _make_pkg(tmp_path, spec_stub,
                               destinations={"artifacts": [], "include_trace": False})
    report = CompletenessVerifier().verify(pkg, manifest)
    v9 = _v9(report)
    assert v9.status == "warn"
    assert report.ok  # warn is not fail


def test_collect_api_key_envs_sweeps_tool_modules(tmp_path):
    """A secret consumed by a TOOL module (e.g. research.tools.web's
    TAVILY_API_KEY) must be collectable — otherwise the package can never
    declare it and fail-closed runners won't inject it."""
    from armature.spec.loader import load_spec
    from armature.packaging.verifier import collect_api_key_envs

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openrouter\n"
        "    model: m\n"
        "    api_key_env: OPENROUTER_API_KEY\n"
        "tools:\n"
        "  - module: research.tools.web\n"
        "    api_key_env: [TAVILY_API_KEY]\n"
        "stages:\n"
        "  - id: s1\n"
        "    role: {name: W, type: worker, description: hi}\n",
        encoding="utf-8",
    )
    spec = load_spec(spec_file)
    envs = collect_api_key_envs(spec)
    assert envs == {"OPENROUTER_API_KEY", "TAVILY_API_KEY"}
