# tests/packaging/test_runner.py
import json
from pathlib import Path
from ruamel.yaml import YAML
from armature.packaging.builder import PackageBuilder
from armature.packaging.runner import PackageRunner, SecretMissingError

_Y = YAML()


class FakeTraces:
    async def query_by_run(self, run_id): return []

class FakeHarness:
    def __init__(self, spec, session_dir, traces_db):
        self._spec = spec
        self._run_id = "testrun0001"
        self._traces = FakeTraces()
    async def run(self, inputs, force=False):
        return {s.id: {"content": f"out for {inputs.get('topic','')}"}
                for s in self._spec.stages if s.role}

def _build_pkg(tmp_path, tiny_spec):
    return PackageBuilder().build(spec=tiny_spec, out=tmp_path / "echo.pkg",
                                  inputs={"topic": "hello"})

def test_runner_complete(tmp_path, tiny_spec, monkeypatch):
    pkg = _build_pkg(tmp_path, tiny_spec)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    runner = PackageRunner(harness_factory=FakeHarness, skip_deps_install=True)
    receipt = runner.run_sync(pkg, tmp_path / "results")
    assert receipt.status == "complete"
    assert (tmp_path / "results" / "testrun0001" / "receipt.json").exists()
    assert (tmp_path / "results" / "testrun0001" / "artifacts" / "writer.md").exists()

def test_runner_secrets_fail_closed(tmp_path, tiny_spec, monkeypatch):
    pkg = _build_pkg(tmp_path, tiny_spec)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = PackageRunner(harness_factory=FakeHarness, skip_deps_install=True)
    try:
        runner.run_sync(pkg, tmp_path / "results")
    except SecretMissingError:
        return
    raise AssertionError("expected SecretMissingError")

def test_runner_input_override(tmp_path, tiny_spec, monkeypatch):
    pkg = _build_pkg(tmp_path, tiny_spec)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    runner = PackageRunner(harness_factory=FakeHarness, skip_deps_install=True)
    receipt = runner.run_sync(pkg, tmp_path / "results", inputs_override={"topic": "override"})
    art = (tmp_path / "results" / "testrun0001" / "artifacts" / "writer.md").read_text()
    assert "override" in art

# ── subagent packages run from an unrelated cwd (Fargate acceptance) ───────────

_SUBAGENT_PARENT = """\
name: subagent-demo
version: "1.0"
description: Parent workflow with a subagent stage.
contracts:
  inputs: []
stages:
  - id: spawn
    subagent_spec: workflows/child.yaml
"""

_ECHO_CHILD = """\
name: child
version: "1.0"
description: No-LLM child (script adapter) for package e2e.
adapters:
  greet:
    name: greet
    type: script
    cmd: "echo 'child says: {{greeting}}'"
stages:
  - id: respond
    adapter: greet
"""


def test_package_run_with_subagent_from_unrelated_cwd(tmp_path, monkeypatch):
    """Acceptance (armature-dispatch/Fargate): build a package whose workflow
    spawns a subagent, then run it with the process cwd outside both the
    package and the repo — the bundled child spec is what must execute."""
    repo = tmp_path / "repo"
    (repo / "workflows").mkdir(parents=True)
    (repo / "workflows" / "child.yaml").write_text(_ECHO_CHILD, encoding="utf-8")
    spec_path = repo / "workflow.yaml"
    spec_path.write_text(_SUBAGENT_PARENT, encoding="utf-8")

    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "pkg", inputs={})

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)  # not the repo, not the package

    runner = PackageRunner(skip_deps_install=True)
    receipt = runner.run_sync(pkg, tmp_path / "results")

    assert receipt.status == "complete", receipt.error
    spawn_artifacts = list((tmp_path / "results").glob("*/artifacts/spawn.md"))
    assert spawn_artifacts, "expected the subagent stage artifact"
    art = spawn_artifacts[0].read_text(encoding="utf-8")
    assert "child says" in art  # the bundled child actually ran


def test_package_run_captures_artifact_files_from_workdir(tmp_path, monkeypatch):
    """End-to-end (dispatch): a packaged workflow whose tool writes a real
    file (research-output/report.html) declares it as a file-capture
    destination; the run delivers the actual file inside results/ and the
    receipt digests it — not a 105-byte pointer."""
    spec_file = tmp_path / "workflow.yaml"
    spec_file.write_text(
        "name: filecap\n"
        "destinations:\n"
        "  artifacts:\n"
        "    - stage_id: writer\n"
        "      name: report\n"
        "      format: text\n"
        "      source: research-output/report.html\n"
        "adapters:\n"
        "  wt:\n"
        "    name: wt\n"
        "    type: script\n"
        "    cmd: \"mkdir -p research-output && echo 'real report body' > research-output/report.html\"\n"
        "stages:\n"
        "  - id: writer\n"
        "    adapter: wt\n",
        encoding="utf-8",
    )
    pkg = PackageBuilder().build(spec=spec_file, out=tmp_path / "pkg", inputs={})

    job_dir = tmp_path / "job"  # the run's working directory (Fargate: /job)
    job_dir.mkdir()
    monkeypatch.chdir(job_dir)

    receipt = PackageRunner(skip_deps_install=True).run_sync(pkg, tmp_path / "results")

    assert receipt.status == "complete", receipt.error
    captured = list((tmp_path / "results").glob("*/artifacts/report.html"))
    assert captured, "expected the captured file inside results/"
    assert "real report body" in captured[0].read_text(encoding="utf-8")
    entry = next(a for a in receipt.artifacts if a.path == "artifacts/report.html")
    assert entry.sha256  # receipt digests the delivered bytes
