"""Tests for `armature watch` CLI command."""
import pytest
from typer.testing import CliRunner
from armature.cli import app

runner = CliRunner()


def test_watch_command_registered():
    result = runner.invoke(app, ["watch", "--help"])
    assert result.exit_code == 0
    assert "watch" in result.output.lower() or "spec" in result.output.lower()


def test_watch_command_errors_on_missing_spec(tmp_path):
    missing = str(tmp_path / "nonexistent.yaml")
    result = runner.invoke(app, ["watch", missing])
    assert result.exit_code != 0


def test_watch_tune_flag_is_skeleton(tmp_path):
    spec = tmp_path / "wf.yaml"
    spec.write_text(
        "name: wf\n"
        "model_tiers:\n"
        "  small:\n"
        "    provider: openai\n"
        "    model: gpt-4o-mini\n"
        "triggers:\n"
        "  - type: cron\n"
        "    schedule: '0 0 * * *'\n"
        "stages:\n"
        "  - id: s\n"
        "    depends_on: []\n"
        "    role:\n"
        "      name: R\n"
        "      type: worker\n"
        "      description: d\n"
    )
    result = runner.invoke(app, ["watch", str(spec), "--tune"])
    assert result.exit_code == 0, result.output
    assert "skeleton" in result.output.lower()
