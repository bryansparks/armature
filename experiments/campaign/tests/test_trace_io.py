import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from campaign_runner import trace_io


def _build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE traces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL, workflow_name TEXT NOT NULL, stage_id TEXT NOT NULL,
          role_type TEXT NOT NULL, model TEXT NOT NULL,
          input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
          latency_ms REAL DEFAULT 0.0, success INTEGER NOT NULL DEFAULT 1,
          output_valid INTEGER NOT NULL DEFAULT 1, quorum_score REAL,
          timestamp TEXT NOT NULL, inputs_json TEXT DEFAULT '{}',
          outputs_json TEXT DEFAULT '{}', error_type TEXT, error_kind TEXT,
          escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '',
          loop_iteration INTEGER, agent_id TEXT, agent_version TEXT,
          active_skill_ids_json TEXT DEFAULT '[]'
        );
    """)
    rows = [
        ("r1", "wf", "s1", "worker", "m", 10, 20, 1200.0, 1, 1, 0.8, "2026-01-01T00:00:01", 0),
        ("r1", "wf", "s2", "judge",  "m", 5,  30, 3000.0, 1, 1, 0.9, "2026-01-01T00:00:02", 1),
        ("r2", "wf", "s1", "worker", "m", 10, 20, 500.0,  1, 1, None,"2026-01-01T00:00:03", 0),
        ("ls", "wf", "__loop__", "orchestrator", "loop-driver", 0,0,0.0,1,1,None,"2026-01-01T00:00:04",0),
    ]
    con.executemany(
        "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
        "input_tokens,output_tokens,latency_ms,success,output_valid,quorum_score,"
        "timestamp,escalation_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def _build_db_with_error_kind(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE traces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL, workflow_name TEXT NOT NULL, stage_id TEXT NOT NULL,
          role_type TEXT NOT NULL, model TEXT NOT NULL,
          input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
          latency_ms REAL DEFAULT 0.0, success INTEGER NOT NULL DEFAULT 1,
          output_valid INTEGER NOT NULL DEFAULT 1, quorum_score REAL,
          error_kind TEXT, timestamp TEXT NOT NULL,
          escalation_count INTEGER DEFAULT 0);
    """)
    con.executemany(
        "INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,"
        "input_tokens,output_tokens,latency_ms,success,output_valid,quorum_score,"
        "error_kind,timestamp,escalation_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("r1", "wf", "s1", "worker", "m", 10, 20, 1200.0, 0, 0, None,
          "provider_credits", "2026-01-01T00:00:01", 0),
         ("r1", "wf", "s2", "judge",  "m", 5,  30, 3000.0, 1, 1, 0.9,
          None, "2026-01-01T00:00:02", 1)],
    )
    con.commit(); con.close()


def test_read_rows_by_run(tmp_path):
    db = tmp_path / "traces.sqlite"
    _build_db(db)
    rows = trace_io.read_rows_by_run(db, "r1")
    assert [r.stage_id for r in rows] == ["s1", "s2"]
    assert rows[0].success is True and rows[0].output_valid is True
    assert rows[0].quorum_score == 0.8
    assert rows[1].escalation_count == 1
    assert rows[1].latency_ms == 3000.0


def test_latest_run_id_excludes_loop_summary(tmp_path):
    db = tmp_path / "traces.sqlite"
    _build_db(db)
    # the most recent non-__loop__ run is r2
    assert trace_io.latest_run_id(db, "wf") == "r2"
    assert trace_io.list_runs(db, "wf") == ["r1", "r2"]


def test_improve_log_and_history_readers(tmp_path):
    log = tmp_path / "spec.improve_log.jsonl"
    log.write_text(json.dumps({"n_traces": 5, "hqs_before": 0.6,
                               "needs_improvement": True}) + "\n"
                   + json.dumps({"n_traces": 6, "hqs_before": 0.8,
                                 "needs_improvement": False}) + "\n")
    rows = trace_io.read_improve_log(log)
    assert len(rows) == 2 and rows[0]["needs_improvement"] is True

    hist = tmp_path / "spec.spec_history.jsonl"
    hist.write_text(json.dumps({"yaml": "old"}) + "\n")
    assert trace_io.read_spec_history(hist) == [{"yaml": "old"}]


def test_read_pending_returns_none_when_absent(tmp_path):
    assert trace_io.read_pending(tmp_path / "nope.pending.yaml") is None
    p = tmp_path / "x.pending.yaml"
    p.write_text("stages_added: []\n")
    assert trace_io.read_pending(p) == "stages_added: []\n"


def test_total_tokens_sums_input_and_output(tmp_path):
    db = tmp_path / "traces.sqlite"
    _build_db(db)
    # r1: 10+20 + 5+30 = 65; r2: 10+20 = 30; ls: 0+0 = 0 → total 95
    assert trace_io.total_tokens(db) == 95


def test_total_tokens_zero_when_no_db(tmp_path):
    assert trace_io.total_tokens(tmp_path / "nonexistent.sqlite") == 0


def _tr(role_type):
    return trace_io.TraceRow(run_id="r", workflow_name="wf", stage_id="s",
                              role_type=role_type, model="m", input_tokens=0,
                              output_tokens=0, latency_ms=1.0, success=True,
                              output_valid=True, quorum_score=0.5, escalation_count=0)


def test_count_agent_spawns_counts_llm_role_rows():
    """count_agent_spawns counts trace rows whose role_type is an LLM stage
    (worker/researcher/judge/orchestrator) — one per agent invocation, incl.
    fan-out partitions and retries. gate/script/adapter rows are excluded."""
    rows = [_tr("researcher"), _tr("researcher"), _tr("judge"), _tr("gate")]
    assert trace_io.count_agent_spawns(rows) == 3


def test_count_agent_spawns_accepts_dicts_too():
    """concurrency summaries hold trace rows as dicts (asdict(TraceRow)); the
    helper must count those the same way."""
    rows = [{"role_type": "worker"}, {"role_type": "orchestrator"},
            {"role_type": "script"}, {"role_type": "adapter"}]
    assert trace_io.count_agent_spawns(rows) == 2


def test_count_agent_spawns_empty():
    assert trace_io.count_agent_spawns([]) == 0


def test_read_rows_by_run_reads_error_kind(tmp_path):
    db = tmp_path / "traces.sqlite"
    _build_db_with_error_kind(db)
    rows = trace_io.read_rows_by_run(db, "r1")
    assert rows[0].error_kind == "provider_credits"
    assert rows[1].error_kind is None


def test_account_scoped_rows_filters_by_error_kind():
    r0 = trace_io.TraceRow(run_id="r", workflow_name="wf", stage_id="s", role_type="worker",
                           model="m", input_tokens=0, output_tokens=0, latency_ms=1.0,
                           success=False, output_valid=False, quorum_score=None,
                           escalation_count=0, error_kind="provider_credits")
    r1 = trace_io.TraceRow(run_id="r", workflow_name="wf", stage_id="s", role_type="judge",
                           model="m", input_tokens=0, output_tokens=0, latency_ms=1.0,
                           success=True, output_valid=True, quorum_score=0.9,
                           escalation_count=0, error_kind=None)
    scoped = trace_io.account_scoped_rows([r0, r1])
    assert scoped == [r0]