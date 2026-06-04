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
