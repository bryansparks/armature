import json
from pathlib import Path
import typer
from typer.testing import CliRunner
from armature.cli import app

runner = CliRunner()


def test_loop_command_help_lists_flags():
    # `--help` must exit 0 (the command is wired up and renders help).
    result = runner.invoke(app, ["loop", "--help"])
    assert result.exit_code == 0

    # Assert the flags are REGISTERED on the loop command by introspecting the
    # click params directly, not by substring-matching the rendered --help text.
    # Typer's rich help rendering (boxed panels, column truncation with ellipses)
    # varies across typer/rich versions and terminal widths — `typer>=0.12` is
    # unpinned, so CI installs a version that renders `--max-iterations`
    # differently than local, breaking a naive `flag in result.stdout` assertion.
    # The registered options are the stable, version-independent truth.
    loop_cmd = typer.main.get_command(app).commands["loop"]
    registered = {opt for p in loop_cmd.params for opt in p.opts}
    for flag in ["--max-iterations", "--max-llm-calls", "--until",
                 "--carry-forward", "--converge", "--interval", "--output"]:
        assert flag in registered, f"{flag} not registered on loop command"


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
