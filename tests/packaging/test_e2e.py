# tests/packaging/test_e2e.py
from pathlib import Path
from armature.packaging.builder import PackageBuilder
from armature.packaging.runner import PackageRunner

def test_build_and_run_direct_no_llm(tmp_path, no_llm_pkg):
    spec_path, tools = no_llm_pkg
    pkg = PackageBuilder().build(spec=spec_path, out=tmp_path / "echo.pkg",
                                 inputs={}, tools=tools)
    # no api_key_env declared -> no secrets required -> runs without a profile.
    # The runner's _install_tools_dir is unconditional (R4) and puts the vendored
    # tools dir on sys.path before the Harness is constructed and tool modules
    # are imported (R6), so no manual sys.path insert is needed here.
    runner = PackageRunner(skip_deps_install=True)
    receipt = runner.run_sync(pkg, tmp_path / "results", include_trace=True)
    assert receipt.status == "complete"
    run_dir = tmp_path / "results" / receipt.run_id
    assert (run_dir / "receipt.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    # the echo stage produced an artifact named after the leaf stage
    assert any(a.stage_id == "echo" for a in receipt.artifacts)