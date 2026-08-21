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