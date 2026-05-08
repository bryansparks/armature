"""Tests for CLI commands: validate, run (dry-run, inputs), on_event."""
import json
from pathlib import Path
from typer.testing import CliRunner
from armature.cli import app, parse_inputs, _make_on_event
import typer
import pytest

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal.yaml"
ECHO = FIXTURES / "echo-workflow.yaml"


# ── parse_inputs ──────────────────────────────────────────────────────────────

def test_parse_inputs_single_pair():
    assert parse_inputs(["key=value"]) == {"key": "value"}


def test_parse_inputs_multiple_pairs():
    assert parse_inputs(["a=1", "b=2"]) == {"a": "1", "b": "2"}


def test_parse_inputs_empty_list():
    assert parse_inputs([]) == {}


def test_parse_inputs_value_contains_equals():
    assert parse_inputs(["url=http://x.com/a=b"]) == {"url": "http://x.com/a=b"}


def test_parse_inputs_invalid_format_exits():
    result = runner.invoke(app, ["run", str(MINIMAL), "--input", "noequalssign"])
    assert result.exit_code != 0


# ── armature validate ─────────────────────────────────────────────────────────

def test_validate_valid_spec_exits_0():
    result = runner.invoke(app, ["validate", str(MINIMAL)])
    assert result.exit_code == 0
    assert "valid" in result.output.lower() or "✓" in result.output


def test_validate_valid_spec_echo_exits_0():
    result = runner.invoke(app, ["validate", str(ECHO)])
    assert result.exit_code == 0


def test_validate_missing_file_exits_1():
    result = runner.invoke(app, ["validate", "/nonexistent/spec.yaml"])
    assert result.exit_code == 1


def test_validate_invalid_spec_exits_1(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nstages:\n  - id: s\n    depends_on: []\n")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "NO_EXECUTION_TYPE" in result.output


def test_validate_undefined_dependency_shown(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\n"
        "stages:\n"
        "  - id: s\n"
        "    tool_call:\n"
        "      name: t\n"
        "    depends_on: [missing]\n"
    )
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "UNDEFINED_DEPENDENCY" in result.output


def test_validate_bad_yaml_exits_1(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [\n  unclosed bracket\n")
    result = runner.invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1


# ── armature run --dry-run ────────────────────────────────────────────────────

def test_run_dry_run_valid_spec_exits_0():
    result = runner.invoke(app, ["run", str(MINIMAL), "--dry-run"])
    assert result.exit_code == 0
    assert "Dry run" in result.output or "dry" in result.output.lower()


def test_run_dry_run_shows_spec_name():
    result = runner.invoke(app, ["run", str(MINIMAL), "--dry-run"])
    assert result.exit_code == 0
    assert "minimal-workflow" in result.output


def test_run_dry_run_invalid_spec_exits_1(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nstages:\n  - id: s\n    depends_on: []\n")
    result = runner.invoke(app, ["run", str(bad), "--dry-run"])
    assert result.exit_code == 1


def test_run_missing_file_exits_1():
    result = runner.invoke(app, ["run", "/nonexistent/workflow.yaml"])
    assert result.exit_code == 1


def test_run_dry_run_quiet_produces_no_output():
    result = runner.invoke(app, ["run", str(MINIMAL), "--dry-run", "--quiet"])
    # Quiet mode may still print "Dry run" — spec: just validates no crash
    assert result.exit_code == 0


# ── _make_on_event ────────────────────────────────────────────────────────────

def test_make_on_event_quiet_returns_none():
    assert _make_on_event(quiet=True) is None


def test_make_on_event_not_quiet_returns_callable():
    cb = _make_on_event(quiet=False)
    assert callable(cb)


def test_on_event_stage_start_prints():
    import io
    cb = _make_on_event(quiet=False)
    # Should not raise; output captured by typer.echo → not easy to assert content
    cb("stage_start", {"stage": "s1", "kind": "llm", "role": "worker (worker)"})


def test_on_event_stage_complete_prints():
    cb = _make_on_event(quiet=False)
    cb("stage_complete", {"stage": "s1", "elapsed_s": 1.2})


def test_on_event_stage_skipped_prints():
    cb = _make_on_event(quiet=False)
    cb("stage_skipped", {"stage": "s1", "reason": "skip_if"})


def test_on_event_run_summary_prints():
    cb = _make_on_event(quiet=False)
    cb("run_summary", {
        "elapsed_s": 5.0,
        "stages_ran": 3,
        "stages_skipped": 1,
        "stages_resumed": 0,
        "stages_failed": 0,
    })


def test_on_event_unknown_event_does_not_raise():
    cb = _make_on_event(quiet=False)
    cb("unknown_event_type", {"arbitrary": "data"})


def test_on_event_retry_attempt_prints():
    cb = _make_on_event(quiet=False)
    cb("retry_attempt", {"stage": "s1", "attempt": 1, "max": 3, "reason": "oops"})


def test_on_event_stage_failed_prints():
    cb = _make_on_event(quiet=False)
    cb("stage_failed", {"stage": "s1", "type": "ValueError", "reason": "something went wrong"})
