# tests/packaging/test_pkg_manifest.py — renamed to avoid a basename collision
# with tests/adapters/test_manifest.py under pytest's default import mode.
from armature.packaging.manifest import (
    PackageManifest, SecretsFile, SecretRequirement, Destinations, ArtifactSpec,
    ResultsManifest, TraceRef, PACKAGE_API_VERSION,
)

def test_package_manifest_defaults():
    m = PackageManifest(
        name="demo", version="1.0", armature_version=">=0.6.0",
        created_at="2026-08-21T12:00:00Z",
    )
    assert m.api_version == PACKAGE_API_VERSION == "armature.package/v1"
    assert m.spec == "workflow.yaml"
    assert m.secrets == "secrets.yaml"
    assert m.destinations == "destinations.yaml"
    assert m.integrity == "manifest.sha256"
    assert m.runtime_inputs == []

def test_secrets_and_destinations_roundtrip():
    s = SecretsFile(required=[SecretRequirement(name="OPENROUTER_API_KEY", providers=["openrouter"])])
    d = Destinations(
        artifacts=[ArtifactSpec(stage_id="writer", name="briefing", format="markdown")],
        include_trace=True,
    )
    assert s.required[0].name == "OPENROUTER_API_KEY"
    assert d.artifacts[0].format == "markdown"
    assert d.include_trace is True

def test_results_manifest_serializes():
    r = ResultsManifest(
        package_name="demo", package_version="1.0", run_id="abc123",
        status="complete", started_at="t0", finished_at="t1", duration_s=1.0,
        exit_code=0, armature_version="0.6.0", trace=TraceRef(included=True, path="trace.jsonl"),
    )
    js = r.model_dump_json()
    assert '"status":"complete"' in js
    assert '"trace":{"included":true' in js