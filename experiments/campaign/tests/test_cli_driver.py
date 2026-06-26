import json
import sys
import textwrap
from pathlib import Path
import pytest
from campaign_runner import cli_driver, record
from campaign_runner.sandbox import Sandbox
from campaign_runner.plan import load_plan


def _plan(tmp_path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 1}
        phases: [{id: p, lever: none, inputs: {}, repeats: 1}]
        verdicts: {}
    """))
    return load_plan(p)


def test_run_invokes_armature_and_captures_run_id(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    rec = record.Recording(sb.dir / "recording")
    drv = cli_driver.CliDriver(sb, rec)

    captured = {}
    class FakeResult:
        returncode = 0
        def __init__(self, stdout, stderr): self.stdout = stdout; self.stderr = stderr

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        # the driver passes --output <out.json>; write a fake result there
        out = cmd[cmd.index("--output") + 1]
        # write result with run_id
        Path(out).write_text(json.dumps({"run_id": "abc12345", "content": "ok"}))
        # also fake a trace row so latest_run_id fallback works
        import sqlite3
        con = sqlite3.connect(sb.trace_db)
        con.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '', loop_iteration INTEGER, agent_id TEXT, agent_version TEXT, active_skill_ids_json TEXT DEFAULT '[]')")
        con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp) VALUES (?,?,?,?,?,?)", ("abc12345","sample-workflow","s1","worker","m","2026-01-01T00:00:01"))
        con.commit(); con.close()
        return FakeResult("quiet-stdout", "")

    monkeypatch.setattr(cli_driver.subprocess, "run", fake_run)
    spec = tmp_path / "ws.yml"
    spec.write_text("name: x\n")
    out = drv.run(spec, {"topic": "q"})
    assert out.run_id == "abc12345"
    assert out.exit_code == 0
    # HOME was redirected
    assert captured["env"]["HOME"] == str(sb.dir)
    # recording captured the argv
    assert rec.replay()[0]["argv"] == captured["cmd"]


def test_validate_true_on_exit0(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    drv = cli_driver.CliDriver(sb, None)
    class R:
        returncode = 0
    monkeypatch.setattr(cli_driver.subprocess, "run", lambda *a, **k: R())
    assert drv.validate(tmp_path / "ws.yml") is True