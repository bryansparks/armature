import json
from pathlib import Path
from campaign_runner import record


def test_record_then_replay_round_trips(tmp_path):
    rec = record.Recording(tmp_path / "rec")
    rec.record_run("r1", ["armature", "run", "s.yml"], stdout="ok", stderr="",
                   exit_code=0, trace_rows=[{"run_id": "r1", "stage_id": "s1"}],
                   sidecars={"improve_log": "[]"}, dashboard_json={"current_hqs": 0.8})
    rows = rec.replay()
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r1"
    assert rows[0]["argv"] == ["armature", "run", "s.yml"]
    assert rows[0]["trace_rows"][0]["stage_id"] == "s1"
    assert rows[0]["sidecars"]["improve_log"] == "[]"
    assert rows[0]["dashboard_json"]["current_hqs"] == 0.8


def test_replay_empty_dir_returns_empty(tmp_path):
    rec = record.Recording(tmp_path / "empty")
    assert rec.replay() == []


def test_capture_trace_rows_serializes(tmp_path):
    import sqlite3
    from campaign_runner import trace_io
    db = tmp_path / "t.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT,
        stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1,
        output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT,
        inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT,
        escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '', loop_iteration INTEGER,
        agent_id TEXT, agent_version TEXT, active_skill_ids_json TEXT DEFAULT '[]');
    """)
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,quorum_score,latency_ms) VALUES (?,?,?,?,?,?,?,?)",
                ("r1","wf","s1","worker","m","2026-01-01T00:00:01",0.8,1000.0))
    con.commit(); con.close()
    rows = record.capture_trace_rows(db, "r1")
    assert rows[0]["run_id"] == "r1"
    assert rows[0]["quorum_score"] == 0.8
    assert json.dumps(rows)  # JSON-serializable