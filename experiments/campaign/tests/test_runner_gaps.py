"""Tests for the observability-gap logic in campaign_runner/runner.py and
verdicts.py. These gaps must fire only when they represent a real missing
signal — not as false positives on every run."""
import textwrap
from pathlib import Path

from campaign_runner import cli_driver, runner, trace_io
from campaign_runner.plan import load_plan
from campaign_runner.verdicts import verdict_h3


trace_io_ddl = """
CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT,
 stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0,
 output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER
 DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT,
 inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, error_kind TEXT,
 escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '',
 loop_iteration INTEGER, agent_id TEXT, agent_version TEXT,
 active_skill_ids_json TEXT DEFAULT '[]');
"""


def _seed_trace_db(db: Path, run_id: str, role_type="worker", quorum=0.8):
    import sqlite3
    con = sqlite3.connect(db)
    con.executescript(trace_io_ddl)
    con.execute(
        "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
        "timestamp,quorum_score,latency_ms,success,output_valid,escalation_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "sample-workflow", "s1", role_type, "m", "2026-01-01T00:00:01",
         quorum, 1000.0, 1, 1, 0))
    con.commit(); con.close()


def _runner(tmp_path, monkeypatch, lever, with_self_improve=True):
    si = ("self_improve: {enabled: true, target_hqs: 0.75, min_traces: 1, "
          "max_rounds: 1, apply: false}\n" if with_self_improve else "")
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent(f"""
        name: t
        description: "x"
        workflow: s.yml
        budget: {{max_runs: 5}}
        phases:
          - id: p
            lever: {lever}
            inputs: {{topic: "q"}}
            repeats: 1
            {si}
        verdicts: {{}}
    """))
    plan = load_plan(p)
    src = tmp_path / "src.yml"
    src.write_text("name: sample-workflow\nversion: '1.0'\nstages: []\n")

    class FakeDrv:
        def __init__(self, sb, rec):
            self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def improve(self, spec, **kw):
            # non-empty log that never fires -> currently triggers the firing gap
            return cli_driver.ImproveOutcome(0, "", [{"needs_improvement": False}],
                                               None, False)
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            rid = "probe1" if tag == "probe" else "r1"
            _seed_trace_db(self.sb.trace_db, rid)
            hqs_arm = ({"authoritative": 0.8, "dashboard": None}
                       if tag == "main" else None)
            return cli_driver.RunOutcome(rid, 0, "", "", {"run_id": rid},
                                         hqs_armature=hqs_arm)
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)
    return runner.CampaignRunner(plan, src, root=tmp_path / "out"), plan


# ── 2a. feedback gap ─────────────────────────────────────────────────────

def test_feedback_hqs_not_parsed_from_stderr_and_no_gap(tmp_path, monkeypatch):
    """Armature's hqs_feedback hook prints a conditional prose alert to stderr
    (never a parseable 'HQS: <number>'), so the feedback channel is structurally
    non-comparable. _row_from_run must NOT parse a feedback value from stderr and
    must NOT log a 'feedback HQS via hook stderr' gap — even when stderr contains
    a string that looks parseable."""
    r, _ = _runner(tmp_path, monkeypatch, "none", with_self_improve=False)
    _seed_trace_db(r.sb.trace_db, "r1")
    gaps = []
    row = r._row_from_run(
        "r1", "p", "none", {"topic": "q"}, 0, [], None, "", None,
        run_stderr="[armature] HQS hint: quality below 0.75 — consider refining",
        gaps=gaps, hqs_arm={"authoritative": 0.8, "dashboard": None})
    assert row["hqs_armature"]["feedback"] is None
    assert not any(g["want"] == "feedback HQS via hook stderr" for g in gaps)


def test_feedback_hqs_not_parsed_even_when_stderr_has_parseable_number(
        tmp_path, monkeypatch):
    """Even if stderr did contain 'HQS: 0.5', the feedback channel stays None —
    we no longer treat the hook stderr as a comparable HQS emission."""
    r, _ = _runner(tmp_path, monkeypatch, "none", with_self_improve=False)
    _seed_trace_db(r.sb.trace_db, "r1")
    gaps = []
    row = r._row_from_run(
        "r1", "p", "none", {"topic": "q"}, 0, [], None, "", None,
        run_stderr="some noise HQS: 0.5 more noise",
        gaps=gaps, hqs_arm={"authoritative": 0.8, "dashboard": None})
    assert row["hqs_armature"]["feedback"] is None
    assert not any(g["want"] == "feedback HQS via hook stderr" for g in gaps)


def test_verdict_h3_marks_feedback_as_non_comparable():
    """verdict_h3 must document the feedback channel as non-comparable (Armature
    emits only a conditional prose alert, never a value), and must never compare
    it against ours."""
    rows = [{
        "hqs_ours": {"authoritative": 0.8, "rolling": 0.7, "dashboard": 0.6,
                     "feedback": 0.5},
        "hqs_armature": {"authoritative": 0.8, "rolling": 0.7, "dashboard": 0.6,
                         "feedback": None},
    }, {
        "hqs_ours": {"authoritative": 0.82, "rolling": 0.7, "dashboard": 0.6,
                     "feedback": 0.5},
        "hqs_armature": {"authoritative": 0.82, "rolling": 0.7, "dashboard": 0.6,
                         "feedback": None},
    }]
    name, status, detail = verdict_h3(rows, {"max_abs_delta_le": 0.02})
    assert name == "hqs_formula_consistency"
    assert detail["compared"] == "authoritative"
    assert "feedback" in detail["non_comparable"]
    assert "prose" in detail["non_comparable"]["feedback"].lower()
    # feedback must not leak into the compared authoritative deltas
    assert "feedback" not in detail.get("compared", "")


# ── 2b. self_improve firing gap ──────────────────────────────────────────

def test_firing_gap_not_logged_for_difficulty_ramp(tmp_path, monkeypatch):
    """A difficulty-ramp phase with self_improve enabled that never fires must
    NOT log a 'self_improve firing' gap — above the 0.75 target, not firing is
    correct (the phase tests H1, not H2)."""
    r, plan = _runner(tmp_path, monkeypatch, "input_difficulty_ramp")
    gaps = []
    ws = r.sb.working_spec_for("p")
    r._do_improve(plan.phases[0], {"topic": "q"}, gaps, ws, "wf")
    assert not any(g["want"] == "self_improve firing" for g in gaps)


def test_firing_gap_logged_for_degradation_lever(tmp_path, monkeypatch):
    """A model_tier_degradation phase that never fires MUST still log the gap —
    firing is expected there (the lever exists to drop HQS below target)."""
    r, plan = _runner(tmp_path, monkeypatch, "model_tier_degradation")
    gaps = []
    ws = r.sb.working_spec_for("p")
    r._do_improve(plan.phases[0], {"topic": "q"}, gaps, ws, "wf")
    assert any(g["want"] == "self_improve firing" for g in gaps)


# ── 3. per-run agents_run + workflow_name on each row ────────────────────

def test_row_from_run_counts_agents_run_and_workflow_name(tmp_path, monkeypatch):
    """Each campaign row must carry agents_run (LLM-stage invocations for that
    run, incl. fan-out partitions + retries) and workflow_name, so reports can
    show a per-workflow agent tally. gate rows are excluded."""
    r, _ = _runner(tmp_path, monkeypatch, "none", with_self_improve=False)
    import sqlite3
    con = sqlite3.connect(r.sb.trace_db)
    con.executescript(trace_io_ddl)
    for role in ("researcher", "researcher", "judge", "gate"):
        con.execute(
            "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
            "timestamp,quorum_score,latency_ms,success,output_valid,escalation_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "sample-workflow", "s1", role, "m", "2026-01-01T00:00:01",
             0.8, 1000.0, 1, 1, 0))
    con.commit(); con.close()
    row = r._row_from_run(
        "r1", "p", "none", {"topic": "q"}, 0, [], None, "", None,
        workflow_name="", run_stderr="", gaps=[],
        hqs_arm={"authoritative": 0.8, "dashboard": None})
    assert row["agents_run"] == 3          # 2 researcher + 1 judge; gate excluded
    assert row["workflow_name"] == "sample-workflow"   # falls back to trace row


def test_row_from_run_sets_quorum_ours(tmp_path, monkeypatch):
    """_row_from_run must carry quorum_ours (avg judge quorum over the run's
    trace rows) so verdict H4 v3 judges coverage, not aggregate HQS where the
    latency term masks the memory benefit."""
    from campaign_runner import runner as run_mod, hqs, trace_io
    # 2 judge rows with quorum 0.6 and 0.8 -> avg 0.7
    rows = [
        trace_io.TraceRow(run_id="r1", workflow_name="wf", stage_id="s1",
                          role_type="judge", model="m", input_tokens=0,
                          output_tokens=0, latency_ms=10.0, success=1,
                          output_valid=1, quorum_score=0.6, escalation_count=0,
                          error_kind=None),
        trace_io.TraceRow(run_id="r1", workflow_name="wf", stage_id="s2",
                          role_type="judge", model="m", input_tokens=0,
                          output_tokens=0, latency_ms=10.0, success=1,
                          output_valid=1, quorum_score=0.8, escalation_count=0,
                          error_kind=None),
    ]
    monkeypatch.setattr(trace_io, "read_rows_by_run", lambda db, rid: rows if rid == "r1" else [])
    monkeypatch.setattr(run_mod.hqs, "all_four", lambda rs: {"authoritative": None, "rolling": None, "dashboard": None, "feedback": None})
    r = run_mod.CampaignRunner.__new__(run_mod.CampaignRunner)
    r.sb = type("S", (), {"trace_db": tmp_path / "traces.db"})()
    row = r._row_from_run("r1", "p", "none", {}, 0, [], None, "", None)
    assert row["quorum_ours"] == 0.7