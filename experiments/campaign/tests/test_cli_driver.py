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
        captured.setdefault("cmds", []).append(cmd)
        captured["env"] = kw.get("env")
        sub = cmd[1] if len(cmd) > 1 else ""
        # the driver passes --output <out.json>; write a fake result there
        if sub == "run":
            captured["run_cmd"] = cmd
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
        # CliDriver.run now also captures hqs_armature via these calls:
        if sub == "replay":
            return FakeResult("HQS: 0.867", "")             # replay_hqs parses this
        if sub == "dashboard":
            return FakeResult('{"current_hqs": 0.9}', "")   # dashboard_json parses this
        return FakeResult("", "")

    monkeypatch.setattr(cli_driver.subprocess, "run", fake_run)
    spec = tmp_path / "ws.yml"
    spec.write_text("name: x\n")
    out = drv.run(spec, {"topic": "q"})
    assert out.run_id == "abc12345"
    assert out.exit_code == 0
    # --force is passed so every campaign rep is a fresh run (no checkpoint resume)
    assert "--force" in captured["run_cmd"]
    # HOME was redirected (every armature subprocess uses the sandbox env)
    assert captured["env"]["HOME"] == str(sb.dir)
    # recording captured the run argv (not the follow-on replay/dashboard calls)
    assert rec.replay()[0]["argv"] == captured["run_cmd"]
    # the run captured hqs_armature from Armature's own emissions, not a copy of ours
    assert out.hqs_armature == {"authoritative": 0.867, "dashboard": 0.9}
    assert rec.replay()[0]["hqs_armature"] == {"authoritative": 0.867, "dashboard": 0.9}


def test_failed_run_still_recorded(tmp_path, monkeypatch):
    """A run with run_id=None (failure) must still be recorded for replay."""
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    rec = record.Recording(sb.dir / "recording")
    drv = cli_driver.CliDriver(sb, rec)

    class FakeResult:
        returncode = 1
        def __init__(self, stdout, stderr): self.stdout = stdout; self.stderr = stderr

    def fake_run(cmd, **kw):
        # no --output result written, no trace DB → run_id resolves to None
        return FakeResult("partial stdout", "error: bad spec")

    monkeypatch.setattr(cli_driver.subprocess, "run", fake_run)
    spec = tmp_path / "ws.yml"
    spec.write_text("name: x\n")
    out = drv.run(spec, {"topic": "q"})
    assert out.run_id is None
    assert out.exit_code == 1
    # recording still captured the failed run
    rows = rec.replay()
    assert len(rows) == 1
    assert rows[0]["run_id"] is None
    assert rows[0]["exit_code"] == 1


def test_validate_true_on_exit0(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    drv = cli_driver.CliDriver(sb, None)
    class R:
        returncode = 0
    monkeypatch.setattr(cli_driver.subprocess, "run", lambda *a, **k: R())
    assert drv.validate(tmp_path / "ws.yml") is True


def test_parse_hqs_from_text_extracts_float():
    assert cli_driver.parse_hqs_from_text("... HQS: 0.82 (valid=1 ...)") == 0.82


def test_parse_hqs_from_text_none_when_absent():
    assert cli_driver.parse_hqs_from_text("no hqs line here") is None


def test_parse_hqs_from_text_none_on_garbage():
    # regex requires digits; "notanumber" does not match -> None
    assert cli_driver.parse_hqs_from_text("HQS: notanumber") is None


def test_replay_hqs_returns_none_when_no_hqs_line(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    sb = Sandbox(plan, root=tmp_path / "out")
    drv = cli_driver.CliDriver(sb, None)
    class FakeReplay:
        returncode = 0
        stdout = "replay output with no HQS"
        stderr = ""
    monkeypatch.setattr(cli_driver.subprocess, "run", lambda *a, **k: FakeReplay())
    assert drv.replay_hqs("some-run") is None