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
 error_type TEXT, error_kind TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '',
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
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            _fake_trace_db(self.sb.trace_db, "r1")
            # main runs emit the hqs_armature the real CliDriver now captures;
            # probes (tag="probe") return None — recovery uses trace rows.
            hqs_arm = ({"authoritative": 0.8,
                        "dashboard": (self.dashboard_json(workflow_name) or {}).get("current_hqs")}
                       if tag == "main" else None)
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"},
                                         hqs_armature=hqs_arm)
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
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            import sqlite3
            rid = "r1" if tag == "main" else "probe1"
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript(trace_io_ddl.replace(
                "CREATE TABLE traces", "CREATE TABLE IF NOT EXISTS traces"))
            con.execute(
                "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
                "timestamp,quorum_score,latency_ms,success,output_valid,escalation_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:01",
                 0.8, 1000.0, 1, 1, 0))
            con.commit(); con.close()
            hqs_arm = {"authoritative": 0.8, "dashboard": None} if tag == "main" else None
            return cli_driver.RunOutcome(rid, 0, "", "", {"run_id": rid}, hqs_armature=hqs_arm)
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
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
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


def test_replay_folds_probes_and_restores_hqs_armature(tmp_path, monkeypatch):
    """Replay must fold a tagged recovery probe into its parent main row's
    recovery_hqs_ours (not emit a standalone row) and restore hqs_armature
    from the recording — reproducing the live row count + Armature emissions."""
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    sb = Sandbox(plan, root=tmp_path / "out")
    rec = record.Recording(sb.dir / "recording")

    main_rows = [{"run_id":"r1","workflow_name":"wf","stage_id":"s1","role_type":"worker",
                  "model":"m","input_tokens":0,"output_tokens":0,"latency_ms":1000.0,
                  "success":True,"output_valid":True,"quorum_score":0.8,"escalation_count":0}]
    probe_rows = [{"run_id":"p1","workflow_name":"wf","stage_id":"s1","role_type":"worker",
                   "model":"m","input_tokens":0,"output_tokens":0,"latency_ms":1000.0,
                   "success":True,"output_valid":True,"quorum_score":0.9,"escalation_count":0}]
    # main run — hqs_armature captured at record time (Armature's own emissions)
    rec.record_run("r1", ["armature", "run"], "", "", 0, main_rows, {},
                   {"current_hqs": 0.81}, tag="main",
                   hqs_armature={"authoritative": 0.77, "dashboard": 0.81})
    # recovery probe — tagged, must be folded, must NOT become its own row
    rec.record_run("p1", ["armature", "run"], "", "", 0, probe_rows, {},
                   {}, tag="probe", hqs_armature=None)

    class FakeDrv:
        def __init__(self, sb, rec): pass
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    result = r.replay(sb.dir / "recording")
    # probe folded → exactly one row (reproduces the live row count)
    assert len(result.rows) == 1
    row = result.rows[0]
    # hqs_armature restored from the recording — not recomputed, not None
    assert row["hqs_armature"]["authoritative"] == 0.77
    assert row["hqs_armature"]["dashboard"] == 0.81
    assert row["hqs_armature"]["feedback"] is None
    # recovery_hqs_ours computed from the probe's trace rows
    assert row["recovery_hqs_ours"] is not None
    assert abs(row["recovery_hqs_ours"]["authoritative"]
               - hqs.compute_authoritative(
                   [trace_io.TraceRow(**probe_rows[0])])) < 1e-9


def test_break_judge_tier_adds_broken_tier_and_downgrades_judge(tmp_path):
    """The model_tier_degradation lever must add a broken tier and point the
    judge at it, so the judge's LLM call reliably errors and HQS drops."""
    from campaign_runner import fault
    spec = tmp_path / "ws.yml"
    spec.write_text(textwrap.dedent("""
        name: wf
        version: "1.0"
        model_tiers:
          small: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY}
          large: {provider: openrouter, model: z-ai/glm-5.2, api_key_env: OPENROUTER_API_KEY}
        role_type_defaults: {worker: small, judge: large}
        contracts: {inputs: [{name: topic}]}
        stages:
          - id: researcher
            role: {name: Researcher, type: researcher, description: "x"}
            output_mode: text
            depends_on: []
          - id: judge
            role: {name: Judge, type: judge, description: "x"}
            output_mode: guided_json
            output_schema: {type: object, required: [accept], properties: {accept: {type: boolean}}}
            depends_on: [researcher]
    """).strip() + "\n")
    fault._break_judge_tier(spec)
    import yaml
    parsed = yaml.safe_load(spec.read_text())
    assert parsed["model_tiers"]["broken"]["model"] == fault._BROKEN_MODEL
    judge = next(s for s in parsed["stages"] if s["id"] == "judge")
    assert judge["role"]["model_tier"] == "broken"
    # researcher untouched
    researcher = next(s for s in parsed["stages"] if s["id"] == "researcher")
    assert "model_tier" not in researcher.get("role", {})


def test_fresh_db_resets_trace_db_before_phase(tmp_path, monkeypatch):
    """fresh_db:true must delete the trace DB before the phase's reps so prior
    phases' traces don't dilute this phase's hqs_before."""
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 5}
        phases:
          - id: iso
            lever: none
            inputs: {topic: "q"}
            repeats: 1
            fresh_db: true
        verdicts: {}
    """))
    plan = load_plan(p)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")

    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            _fake_trace_db(self.sb.trace_db, "r1")
            hqs_arm = {"authoritative": 0.8, "dashboard": None} if tag == "main" else None
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"}, hqs_armature=hqs_arm)
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")

    # pre-seed the runner's own trace DB with a SENTINEL row from a "prior phase"
    import sqlite3
    con = sqlite3.connect(r.sb.trace_db)
    con.executescript(trace_io_ddl.replace("CREATE TABLE traces",
                                           "CREATE TABLE IF NOT EXISTS traces"))
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp) VALUES (?,?,?,?,?,?)",
                ("SENTINEL", "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:00"))
    con.commit(); con.close()

    # spy on the runner's own sandbox.reset_trace_db so we observe the call and
    # can assert the sentinel is gone the instant the reset fires.
    reset_seen = {"called": False}
    real_reset = r.sb.reset_trace_db
    def spy_reset():
        reset_seen["called"] = True
        return real_reset()
    r.sb.reset_trace_db = spy_reset

    result = r.run()
    assert reset_seen["called"] is True
    assert len(result.rows) == 1
    # final DB must not carry the sentinel (it was reset before the phase ran)
    con = sqlite3.connect(r.sb.trace_db)
    n = con.execute("SELECT count(*) FROM traces WHERE run_id='SENTINEL'").fetchone()[0]
    con.close()
    assert n == 0


def test_replay_restores_phase_context_and_improve_log(tmp_path, monkeypatch):
    """Replay must restore phase_id/lever/inputs from the main run's meta and
    improve_log from the folded probe's meta — otherwise verdicts computed from
    a replay come back INCONCLUSIVE (the third replay gap)."""
    plan = _plan(tmp_path, lever="model_tier_degradation")
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    sb = Sandbox(plan, root=tmp_path / "out")
    rec = record.Recording(sb.dir / "recording")

    main_rows = [{"run_id":"r1","workflow_name":"wf","stage_id":"s1","role_type":"worker",
                  "model":"m","input_tokens":0,"output_tokens":0,"latency_ms":1000.0,
                  "success":True,"output_valid":True,"quorum_score":0.4,"escalation_count":0}]
    probe_rows = [{"run_id":"p1","workflow_name":"wf","stage_id":"s1","role_type":"worker",
                   "model":"m","input_tokens":0,"output_tokens":0,"latency_ms":1000.0,
                   "success":True,"output_valid":True,"quorum_score":0.9,"escalation_count":0}]
    rec.record_run("r1", ["armature", "run"], "", "", 0, main_rows, {},
                   {"current_hqs": 0.4}, tag="main",
                   hqs_armature={"authoritative": 0.4, "dashboard": 0.4},
                   meta={"phase_id": "degrade_apply", "lever": "model_tier_degradation",
                         "inputs": {"topic": "quantum error correction", "difficulty": "3"}})
    rec.record_run("p1", ["armature", "run"], "", "", 0, probe_rows, {},
                   {}, tag="probe", hqs_armature=None,
                   meta={"phase_id": "degrade_apply", "lever": "model_tier_degradation",
                         "inputs": {"topic": "quantum error correction", "difficulty": "3"},
                         "improve_log": [{"needs_improvement": True, "hqs_before": 0.4}]})

    class FakeDrv:
        def __init__(self, sb, rec): pass
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    result = r.replay(sb.dir / "recording")
    assert len(result.rows) == 1
    row = result.rows[0]
    # phase context restored from the main run's meta (not "replay")
    assert row["phase_id"] == "degrade_apply"
    assert row["lever"] == "model_tier_degradation"
    assert row["inputs"]["topic"] == "quantum error correction"
    # improve_log lifted from the folded probe's meta
    assert row["improve_log"] == [{"needs_improvement": True, "hqs_before": 0.4}]
    # recovery still folded from the probe's trace rows
    assert row["recovery_hqs_ours"] is not None


def test_degradation_lever_in_verdict_h2_set(tmp_path):
    """verdict_h2 must count model_tier_degradation runs (not just spec_corruption)."""
    from campaign_runner import verdicts as v
    assert "model_tier_degradation" in v.DEGRADATION_LEVERS
    rows = [{"lever": "model_tier_degradation", "improve_log": [{"needs_improvement": True}],
             "recovery_hqs_ours": {"authoritative": 0.86}}]
    name, status, detail = v.verdict_h2(rows, {"recovers_above": 0.75})
    assert name == "self_improve_fires_and_recovers"
    assert status == "PASS"
    assert detail["fired"] is True


def test_plan_abort_default(tmp_path):
    plan = _plan(tmp_path)
    assert plan.abort is None  # default: no abort block → default K=3 applies


def test_plan_abort_parsed(tmp_path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 5}
        abort: {on_consecutive_account_errors: 2}
        phases:
          - id: p
            lever: none
            inputs: {topic: "q"}
            repeats: 1
        verdicts: {}
    """))
    plan = load_plan(p)
    assert plan.abort.on_consecutive_account_errors == 2


def test_row_from_run_logs_gap_and_flags_for_account_scoped(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    # seed a run with an account-scoped trace row
    import sqlite3
    con = sqlite3.connect(r.sb.trace_db)
    con.executescript(trace_io_ddl)
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,error_kind,success,output_valid,escalation_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("r1", "wf", "s1", "worker", "openrouter/m", "2026-01-01T00:00:01", "provider_credits", 0, 0, 0))
    con.commit(); con.close()
    gaps = []
    row = r._row_from_run("r1", "p", "none", {}, 1, [], None, "", None,
                          run_stderr="", gaps=gaps, hqs_arm=None, workflow_name="wf")
    assert row["account_scoped"] is True
    assert row["account_scoped_kind"] == "provider_credits"
    assert row["account_scoped_model"] == "openrouter/m"
    assert any(g["want"] == "funded provider account" and g["severity"] == "high" for g in gaps)


def _seed_trace_row(db, run_id, role_type, outputs_json, quorum_score=0.9,
                    stage_id="s1", workflow_name="wf"):
    """Insert a trace row carrying outputs_json (the column the model_failed
    detector reads). Reuses the proven trace_io_ddl shape from _fake_trace_db."""
    import sqlite3
    con = sqlite3.connect(db)
    con.executescript(trace_io_ddl.replace("CREATE TABLE traces",
                                           "CREATE TABLE IF NOT EXISTS traces"))
    con.execute(
        "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,"
        "quorum_score,latency_ms,success,output_valid,escalation_count,outputs_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, workflow_name, stage_id, role_type, "m", "2026-01-01T00:00:01",
         quorum_score, 1000.0, 1, 1, 0, outputs_json))
    con.commit(); con.close()


def test_row_from_run_sets_model_failed_true_for_self_contradictory_judge(tmp_path):
    """A judge row with accept=True and confidence<0.5 is a self-contradictory
    model failure → _row_from_run sets row["model_failed"] is True."""
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    _seed_trace_row(r.sb.trace_db, "r1", "judge",
                    '{"accept":"True","confidence":"0","issues":"[]"}',
                    quorum_score=0.0, stage_id="judge")
    _seed_trace_row(r.sb.trace_db, "r1", "researcher",
                    '{"content":"' + ("x" * 200) + '"}',
                    quorum_score=0.85, stage_id="researcher")
    row = r._row_from_run("r1", "p", "none", {}, 1, [], None, "", None,
                          run_stderr="", gaps=None, hqs_arm=None,
                          workflow_name="wf")
    assert row["model_failed"] is True


def test_row_from_run_sets_model_failed_true_for_empty_researcher(tmp_path):
    """A researcher row whose content is < 40 chars is an empty briefing →
    model_failed True, even when the judge is fine."""
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    _seed_trace_row(r.sb.trace_db, "r1", "researcher", '{"content":"too short briefing"}',
                    quorum_score=0.05, stage_id="researcher")
    _seed_trace_row(r.sb.trace_db, "r1", "judge",
                    '{"accept":"True","confidence":"0.9","issues":"[]"}',
                    quorum_score=0.9, stage_id="judge")
    row = r._row_from_run("r1", "p", "none", {}, 1, [], None, "", None,
                          run_stderr="", gaps=None, hqs_arm=None,
                          workflow_name="wf")
    assert row["model_failed"] is True


def test_row_from_run_sets_model_failed_false_for_clean_run(tmp_path):
    """A clean run: judge accept=True confidence=0.9 AND a 200-char researcher
    content → model_failed is False."""
    plan = _plan(tmp_path)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")
    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    _seed_trace_row(r.sb.trace_db, "r1", "researcher",
                    '{"content":"' + ("y" * 200) + '"}',
                    quorum_score=0.85, stage_id="researcher")
    _seed_trace_row(r.sb.trace_db, "r1", "judge",
                    '{"accept":"True","confidence":"0.9","issues":"[]"}',
                    quorum_score=0.9, stage_id="judge")
    row = r._row_from_run("r1", "p", "none", {}, 1, [], None, "", None,
                          run_stderr="", gaps=None, hqs_arm=None,
                          workflow_name="wf")
    assert row["model_failed"] is False


def test_circuit_breaker_aborts_after_k_consecutive(tmp_path, monkeypatch):
    """K=2 consecutive account-scoped runs → aborted=True, run stops, _finalize
    writes aborted/abort_reason; a good run between resets the streak."""
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 20}
        abort: {on_consecutive_account_errors: 2}
        phases:
          - id: p
            lever: none
            inputs: {topic: "q"}
            repeats: 20
        verdicts: {}
    """))
    plan = load_plan(p)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")
    call = {"n": 0}

    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            call["n"] += 1
            rid = f"r{call['n']}"
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript(trace_io_ddl.replace("CREATE TABLE traces", "CREATE TABLE IF NOT EXISTS traces"))
            # runs 1 and 2 are account-scoped (402); run 3 would not happen
            ek = "provider_credits" if call["n"] <= 2 else None
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,error_kind,success,output_valid,escalation_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:01", ek, 0 if ek else 1, 0 if ek else 1, 0))
            con.commit(); con.close()
            hqs_arm = {"authoritative": None, "dashboard": None} if tag == "main" else None
            return cli_driver.RunOutcome(rid, 1 if ek else 0, "", "", {"run_id": rid}, hqs_armature=hqs_arm)
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return None
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    result = r.run()
    assert r.aborted is True
    assert r.abort_reason == "provider account exhausted"
    assert len(result.rows) == 2           # stopped after the 2nd account-scoped run
    assert call["n"] == 2                  # no 3rd run was spawned
    meta = json.loads((r.sb.dir / "meta.json").read_text())
    assert meta["aborted"] is True


def test_good_run_resets_account_scoped_streak(tmp_path, monkeypatch):
    """A non-account-scoped run between two account-scoped runs resets the
    streak, so K=2 consecutive never trips and the campaign continues."""
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 5}
        abort: {on_consecutive_account_errors: 2}
        phases:
          - id: p
            lever: none
            inputs: {topic: "q"}
            repeats: 5
        verdicts: {}
    """))
    plan = load_plan(p)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")
    call = {"n": 0}

    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            call["n"] += 1
            rid = f"r{call['n']}"
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript(trace_io_ddl.replace("CREATE TABLE traces", "CREATE TABLE IF NOT EXISTS traces"))
            # pattern: scoped, good, scoped, good, good → never 2 consecutive scoped
            scoped = call["n"] in (1, 3)
            ek = "provider_credits" if scoped else None
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,error_kind,success,output_valid,escalation_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, "sample-workflow", "s1", "worker", "m", "2026-01-01T00:00:01", ek, 0 if scoped else 1, 0 if scoped else 1, 0))
            con.commit(); con.close()
            hqs_arm = {"authoritative": 0.8, "dashboard": None} if tag == "main" else None
            return cli_driver.RunOutcome(rid, 1 if scoped else 0, "", "", {"run_id": rid}, hqs_armature=hqs_arm)
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, src, root=tmp_path / "out")
    result = r.run()
    assert r.aborted is False
    assert len(result.rows) == 5