import json
import textwrap
from pathlib import Path
import pytest
from campaign_runner import runner, record, cli_driver, trace_io, hqs
from campaign_runner.plan import load_plan
from campaign_runner.sandbox import Sandbox


def _plan(tmp_path: Path, lever="none"):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent(f"""
        name: t
        description: "x"
        workflow: s.yml
        budget: {{max_runs: 2}}
        phases:
          - id: p
            lever: {lever}
            inputs: {{topic: "q"}}
            repeats: 1
        verdicts: {{}}
    """))
    return load_plan(p)


def _fake_trace_db(db: Path, run_id: str):
    import sqlite3
    con = sqlite3.connect(db)
    con.executescript(trace_io_ddl)
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,quorum_score,latency_ms,success,output_valid,escalation_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:01", 0.8, 1000.0, 1, 1, 0))
    con.commit(); con.close()

trace_io_ddl = """
CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT,
 role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
 latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1,
 quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}',
 error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '',
 loop_iteration INTEGER, agent_id TEXT, agent_version TEXT, active_skill_ids_json TEXT DEFAULT '[]');
"""


def test_run_drives_one_phase_and_writes_campaign_jsonl(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")

    # stub the CLI driver so no real armature call happens
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name=""):
            _fake_trace_db(self.sb.trace_db, "r1")
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"})
        def improve(self, spec, **kw):
            return cli_driver.ImproveOutcome(0, "", [], None, False)
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    result = r.run()
    assert len(result.rows) == 1
    assert result.rows[0]["run_id"] == "r1"
    assert result.campaign_jsonl.exists()
    rows = [json.loads(l) for l in result.campaign_jsonl.read_text().splitlines() if l.strip()]
    assert rows[0]["hqs_ours"]["authoritative"] is not None
    assert result.report_path.exists()


def test_budget_trips_on_llm_calls_including_improve_rounds(tmp_path, monkeypatch):
    """max_llm_calls must count improve rounds + recovery probe, not just main runs."""
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 5, max_llm_calls: 2}
        phases:
          - id: p
            lever: none
            inputs: {topic: "q"}
            repeats: 3
            self_improve: {enabled: true, target_hqs: 0.75, min_traces: 1, max_rounds: 1, apply: false}
        verdicts: {}
    """))
    plan = load_plan(p)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")

    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name=""):
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript(trace_io_ddl.replace(
                "CREATE TABLE traces", "CREATE TABLE IF NOT EXISTS traces"))
            con.execute(
                "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
                "timestamp,quorum_score,latency_ms,success,output_valid,escalation_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("r1", "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:01",
                 0.8, 1000.0, 1, 1, 0))
            con.commit(); con.close()
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"})
        def improve(self, spec, **kw):
            # non-empty log with needs_improvement=False → one round, then break
            return cli_driver.ImproveOutcome(0, "", [{"needs_improvement": False}], None, False)
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    result = r.run()
    # Each rep costs 1 (main) + 1 (improve round) + 1 (probe) = 3 LLM calls.
    # After rep 0: llm_calls=3 >= max_llm_calls=2 → rep 1 budget check trips.
    assert len(result.rows) == 1


def test_budget_trips_on_max_tokens(tmp_path, monkeypatch):
    """max_tokens must be enforced against accumulated trace tokens."""
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 5, max_tokens: 160}
        phases:
          - id: p
            lever: none
            inputs: {topic: "q"}
            repeats: 3
        verdicts: {}
    """))
    plan = load_plan(p)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")

    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name=""):
            # insert a trace row with 100 input + 50 output = 150 tokens per run
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript(trace_io_ddl.replace(
                "CREATE TABLE traces", "CREATE TABLE IF NOT EXISTS traces"))
            con.execute(
                "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
                "timestamp,quorum_score,latency_ms,success,output_valid,escalation_count,"
                "input_tokens,output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r1", "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:01",
                 0.8, 1000.0, 1, 1, 0, 100, 50))
            con.commit(); con.close()
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"})
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    result = r.run()
    # Rep 0: 0 tokens < 160 → OK, run → 150 tokens.
    # Rep 1: 150 < 160 → OK, run → 300 tokens.
    # Rep 2: 300 >= 160 → exceeded! Break.
    assert len(result.rows) == 2


def test_replay_reconstructs_rows_without_armature(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")

    # pre-build a recording with one run
    sb = Sandbox(plan, root=tmp_path / "out")
    rec = record.Recording(sb.dir / "recording")
    rec.record_run("r1", ["armature", "run"], "", "", 0,
                   [{"run_id":"r1","workflow_name":"wf","stage_id":"s1","role_type":"worker",
                     "model":"m","input_tokens":0,"output_tokens":0,"latency_ms":1000.0,
                     "success":True,"output_valid":True,"quorum_score":0.8,"escalation_count":0}],
                   {}, {"current_hqs": 0.8})

    class FakeDrv:
        def __init__(self, sb, rec): pass
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    result = r.replay(sb.dir / "recording")
    assert len(result.rows) == 1
    assert result.rows[0]["run_id"] == "r1"
    assert abs(result.rows[0]["hqs_ours"]["authoritative"]
               - hqs.compute_authoritative([trace_io.TraceRow(**{
                   "run_id":"r1","workflow_name":"wf","stage_id":"s1","role_type":"worker",
                   "model":"m","input_tokens":0,"output_tokens":0,"latency_ms":1000.0,
                   "success":True,"output_valid":True,"quorum_score":0.8,"escalation_count":0})]) ) < 1e-9