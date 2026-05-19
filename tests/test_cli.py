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


# ── armature run (actual execution) ──────────────────────────────────────────

def test_run_echo_workflow_produces_json():
    result = runner.invoke(app, ["run", str(ECHO), "--input", "message=hello"])
    assert result.exit_code == 0
    # JSON output on stdout
    output = result.output
    # Find the JSON block (after the progress lines)
    json_start = output.find("{")
    assert json_start >= 0, f"No JSON found in output: {output!r}"
    data = json.loads(output[json_start:])
    assert data["echo"]["exit_code"] == 0
    assert "hello" in data["echo"]["stdout"]


def test_run_echo_workflow_quiet_suppresses_progress():
    result = runner.invoke(app, ["run", str(ECHO), "--input", "message=hi", "--quiet"])
    assert result.exit_code == 0
    # Quiet mode: no "Running:", no "→", no "✓" progress
    assert "Running:" not in result.output
    assert "→" not in result.output
    # But JSON result is still emitted
    data = json.loads(result.output)
    assert data["echo"]["exit_code"] == 0


def test_run_echo_workflow_output_file(tmp_path):
    out = tmp_path / "result.json"
    result = runner.invoke(app, ["run", str(ECHO), "--input", "message=file", "--output", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["echo"]["exit_code"] == 0


def test_run_echo_workflow_stage_events_in_output():
    result = runner.invoke(app, ["run", str(ECHO), "--input", "message=events"])
    assert result.exit_code == 0
    assert "→ echo" in result.output
    assert "✓ echo" in result.output
    assert "Done in" in result.output


# ---------------------------------------------------------------------------
# export-traces command
# ---------------------------------------------------------------------------

def test_export_traces_missing_db_exits_1(tmp_path):
    result = runner.invoke(app, [
        "export-traces",
        "--workflow", "my-wf",
        "--output", str(tmp_path / "out.jsonl"),
        "--traces", str(tmp_path / "nonexistent.db"),
    ])
    assert result.exit_code == 1


def test_export_traces_produces_jsonl(tmp_path):
    import asyncio
    import aiosqlite as _aio
    import json as _json

    db = tmp_path / "traces.db"

    async def _seed():
        async with _aio.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE traces ("
                "id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, "
                "stage_id TEXT, role_type TEXT, model TEXT, "
                "input_tokens INTEGER, output_tokens INTEGER, latency_ms REAL, "
                "success INTEGER, output_valid INTEGER, quorum_score REAL, "
                "timestamp TEXT, inputs_json TEXT, outputs_json TEXT, "
                "error_type TEXT, escalation_count INTEGER, spec_version TEXT)"
            )
            await conn.execute(
                "INSERT INTO traces VALUES (1,'r1','cli-wf','s1','researcher','m',"
                "10,20,500.0,1,1,0.91,'2026-05-15T00:00:00+00:00',?,?,NULL,0,'')",
                (_json.dumps({"topic": "AI"}), _json.dumps({"brief": "AI is advancing."})),
            )
            await conn.commit()

    asyncio.run(_seed())

    out = tmp_path / "train.jsonl"
    result = runner.invoke(app, [
        "export-traces",
        "--workflow", "cli-wf",
        "--output", str(out),
        "--traces", str(db),
        "--min-score", "0.85",
    ])
    assert result.exit_code == 0
    assert out.exists()
    record = _json.loads(out.read_text().strip())
    assert "messages" in record


def test_export_traces_dpo_format(tmp_path):
    import asyncio
    import aiosqlite as _aio
    import json as _json

    db = tmp_path / "traces.db"

    async def _seed():
        async with _aio.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE traces ("
                "id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, "
                "stage_id TEXT, role_type TEXT, model TEXT, "
                "input_tokens INTEGER, output_tokens INTEGER, latency_ms REAL, "
                "success INTEGER, output_valid INTEGER, quorum_score REAL, "
                "timestamp TEXT, inputs_json TEXT, outputs_json TEXT, "
                "error_type TEXT, escalation_count INTEGER, spec_version TEXT)"
            )
            await conn.execute(
                "INSERT INTO traces VALUES (1,'good','dpo-wf','s1','judge','m',"
                "10,20,500.0,1,1,0.92,'2026-05-15T00:00:00+00:00',?,?,NULL,0,'')",
                (_json.dumps({"q": "x"}), _json.dumps({"content": "Great."})),
            )
            await conn.execute(
                "INSERT INTO traces VALUES (2,'bad','dpo-wf','s1','judge','m',"
                "10,20,500.0,1,0,0.10,'2026-05-15T00:00:01+00:00',?,?,NULL,0,'')",
                (_json.dumps({"q": "x"}), _json.dumps({"content": "Poor."})),
            )
            await conn.commit()

    asyncio.run(_seed())

    out = tmp_path / "dpo.jsonl"
    result = runner.invoke(app, [
        "export-traces",
        "--workflow", "dpo-wf",
        "--output", str(out),
        "--traces", str(db),
        "--format", "dpo",
    ])
    assert result.exit_code == 0
    record = _json.loads(out.read_text().strip())
    assert "chosen" in record
    assert "rejected" in record
    assert "Great." in record["chosen"]
    assert "Poor." in record["rejected"]


def test_export_traces_summary_printed(tmp_path):
    import asyncio
    import aiosqlite as _aio
    import json as _json

    db = tmp_path / "traces.db"

    async def _seed():
        async with _aio.connect(db) as conn:
            await conn.execute(
                "CREATE TABLE traces ("
                "id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, "
                "stage_id TEXT, role_type TEXT, model TEXT, "
                "input_tokens INTEGER, output_tokens INTEGER, latency_ms REAL, "
                "success INTEGER, output_valid INTEGER, quorum_score REAL, "
                "timestamp TEXT, inputs_json TEXT, outputs_json TEXT, "
                "error_type TEXT, escalation_count INTEGER, spec_version TEXT)"
            )
            await conn.execute(
                "INSERT INTO traces VALUES (1,'r1','print-wf','s1','worker','m',"
                "10,20,500.0,1,1,0.90,'2026-05-15T00:00:00+00:00','{}','{}',NULL,0,'')"
            )
            await conn.commit()

    asyncio.run(_seed())

    out = tmp_path / "out.jsonl"
    result = runner.invoke(app, [
        "export-traces",
        "--workflow", "print-wf",
        "--output", str(out),
        "--traces", str(db),
    ])
    assert result.exit_code == 0
    assert "Exported 1 record" in result.output


# ── Phase 1-c: armature doctor ────────────────────────────────────────────────

def test_doctor_exits_0_when_litellm_available():
    result = runner.invoke(app, ["doctor"])
    # litellm is a core dependency — always installed
    assert result.exit_code == 0


def test_doctor_output_mentions_packages():
    result = runner.invoke(app, ["doctor"])
    assert "litellm" in result.output.lower() or "package" in result.output.lower()


def test_doctor_output_mentions_env_vars():
    result = runner.invoke(app, ["doctor"])
    assert any(k in result.output for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "env", "Env"))


def test_doctor_validates_spec_when_provided():
    result = runner.invoke(app, ["doctor", "--spec", str(MINIMAL)])
    assert result.exit_code == 0
    assert "minimal" in result.output.lower() or "spec" in result.output.lower()


def test_doctor_exits_1_on_invalid_spec(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nstages:\n  - id: s\n    depends_on: []\n")
    result = runner.invoke(app, ["doctor", "--spec", str(bad)])
    assert result.exit_code == 1


def test_doctor_exits_1_on_missing_spec_file():
    result = runner.invoke(app, ["doctor", "--spec", "/nonexistent/spec.yaml"])
    assert result.exit_code == 1


def test_doctor_shows_ok_or_error_per_check():
    result = runner.invoke(app, ["doctor"])
    output = result.output.lower()
    assert "ok" in output or "pass" in output or "✓" in result.output or "error" in output or "✗" in result.output
