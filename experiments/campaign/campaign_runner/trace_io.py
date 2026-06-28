"""Raw reads of what Armature wrote: the trace DB + self-improve sidecars.

Read-only. We never write to the trace DB. We reproduce HQS from these rows
ourselves (see hqs.py) rather than trusting Armature's reported numbers.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Role types that correspond to an LLM-stage invocation. Each such invocation
# writes exactly one trace row (fan-out partitions each write their own row,
# and each retry writes one too), so counting rows with these role types == the
# number of agents a run spawned. This mirrors Armature's own
# `llm_calls = len(store.query_by_run(run_id))` (armature/loop/runner.py).
# Excludes `gate` (human pause, no LLM) and `script`/`adapter` (deterministic,
# no LLM). The shared home for this set — runner/concurrency/soak_verdicts/report
# all import it from here.
LLM_ROLE_TYPES = {"worker", "researcher", "judge", "orchestrator"}


@dataclass
class TraceRow:
    run_id: str
    workflow_name: str
    stage_id: str
    role_type: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    success: bool
    output_valid: bool
    quorum_score: float | None
    escalation_count: int
    error_kind: str | None = None


_SELECT = (
    "SELECT run_id, workflow_name, stage_id, role_type, model, "
    "input_tokens, output_tokens, latency_ms, success, output_valid, "
    "quorum_score, escalation_count, error_kind FROM traces"
)


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _row_to_trace(r: sqlite3.Row) -> TraceRow:
    keys = r.keys()
    return TraceRow(
        run_id=r["run_id"], workflow_name=r["workflow_name"], stage_id=r["stage_id"],
        role_type=r["role_type"], model=r["model"],
        input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
        latency_ms=float(r["latency_ms"] or 0.0),
        success=bool(r["success"]), output_valid=bool(r["output_valid"]),
        quorum_score=r["quorum_score"],
        escalation_count=int(r["escalation_count"] or 0),
        error_kind=(r["error_kind"] if "error_kind" in keys and r["error_kind"] else None),
    )


def read_rows_by_run(db_path: Path, run_id: str) -> list[TraceRow]:
    con = _connect(db_path)
    try:
        cur = con.execute(_SELECT + " WHERE run_id = ? ORDER BY timestamp ASC", (run_id,))
        return [_row_to_trace(r) for r in cur.fetchall()]
    finally:
        con.close()


def count_agent_spawns(rows) -> int:
    """Count trace rows that represent an LLM-stage invocation — i.e. the number
    of agents a run spawned. Each LLM stage writes one row, fan-out partitions
    each write their own row, and each retry writes one too, so this equals
    Armature's own `llm_calls = len(store.query_by_run(run_id))`. Excludes
    `gate` (human pause, no LLM) and `script`/`adapter` (deterministic, no LLM).

    Accepts both TraceRow instances (`.role_type`) and dicts (`['role_type']`),
    so the live runner (TraceRow) and the concurrency summaries (asdict dicts)
    share one count path.
    """
    n = 0
    for r in rows:
        rt = r.role_type if hasattr(r, "role_type") else r.get("role_type")
        if rt in LLM_ROLE_TYPES:
            n += 1
    return n


def account_scoped_rows(rows) -> list:
    """Trace rows carrying an account-scoped error_kind bucket (401/402/403-key).
    Accepts TraceRow instances or dicts, like count_agent_spawns."""
    out = []
    for r in rows:
        ek = getattr(r, "error_kind", None)
        if ek is None and isinstance(r, dict):
            ek = r.get("error_kind")
        if ek:
            out.append(r)
    return out


def list_runs(db_path: Path, workflow_name: str) -> list[str]:
    """Run IDs for a workflow, oldest→newest, excluding __loop__ summary rows."""
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT run_id FROM traces WHERE workflow_name = ? "
            "AND stage_id != '__loop__' GROUP BY run_id ORDER BY MIN(timestamp) ASC",
            (workflow_name,),
        )
        return [r["run_id"] for r in cur.fetchall()]
    finally:
        con.close()


def latest_run_id(db_path: Path, workflow_name: str) -> str | None:
    runs = list_runs(db_path, workflow_name)
    return runs[-1] if runs else None


def total_tokens(db_path: Path) -> int:
    """Sum of input+output tokens across all traces (read-only)."""
    if not db_path.exists():
        return 0
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) "
            "FROM traces"
        )
        row = cur.fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def read_improve_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_spec_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read_pending(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text()