import json
from pathlib import Path
from typer.testing import CliRunner
from armature.cli import app

runner = CliRunner()


def test_loop_command_help_lists_flags():
    result = runner.invoke(app, ["loop", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    for flag in ["--max-iterations", "--max-llm-calls", "--until",
                 "--carry-forward", "--converge", "--interval", "--output"]:
        assert flag in out


def test_loop_command_rejects_missing_spec(tmp_path):
    result = runner.invoke(app, ["loop", str(tmp_path / "nope.yaml")])
    assert result.exit_code != 0


FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_loop_command_smoke_one_iteration(tmp_path):
    """End-to-end: one iteration of a real fixture, --output JSON parses."""
    spec_path = FIXTURES / "echo-workflow.yaml"
    if not spec_path.exists():
        import pytest
        pytest.skip("echo-workflow.yaml fixture not present")
    out = tmp_path / "loop.json"
    result = runner.invoke(
        app,
        ["loop", str(spec_path),
         "--input", "message=hello",
         "--max-iterations", "1",
         "--traces", str(tmp_path / "traces.db"),
         "--output", str(out),
         "--quiet"],
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(out.read_text())
    assert data["stop_reason"] == "max_iterations"
    assert len(data["iterations"]) == 1
    assert data["accumulated"]["llm_calls"] >= 0