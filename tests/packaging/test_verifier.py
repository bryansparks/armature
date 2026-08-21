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