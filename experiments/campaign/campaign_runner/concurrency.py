"""Overlapping-firings concurrency stress: N parallel armature subprocesses
against ONE shared trace DB (HOME = sandbox dir). WAL has no busy-retry, so
overlapping writers risk SQLITE_BUSY / row loss — this module surfaces that
as observable summary rows; it does NOT retry (a BUSY crash is a finding)."""
from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path

from campaign_runner import trace_io


def _count_rows(db: Path) -> int:
    if not Path(db).exists():
        return 0
    try:
        con = sqlite3.connect(str(db))
        n = con.execute("SELECT count(*) FROM traces").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def _new_run_ids(db: Path, before: int) -> list[str]:
    """Return run_ids inserted after `before` rows existed (by row id order),
    excluding loop-driver summary rows. `armature loop` writes one __loop__
    trace row per session (LoopRunner._write_summary, run_id=session_id) — that
    is loop control, not a real run, so it must not be counted as a distinct
    run by no_row_loss_under_concurrency. The slice stays on the full ordered
    list (so it stays aligned with the `before` row count, since every trace
    row carries a non-null run_id) and __loop__ rows are filtered out of the
    new-rows slice only."""
    if not Path(db).exists():
        return []
    try:
        con = sqlite3.connect(str(db))
        all_rows = con.execute(
            "SELECT run_id, stage_id FROM traces WHERE run_id IS NOT NULL ORDER BY id").fetchall()
        con.close()
        return [r[0] for r in all_rows[before:] if r[1] != "__loop__"]
    except Exception:
        return []


def run_workers(sb, spec_path: Path, conc, phase_id: str, recording=None) -> list[dict]:
    """Spawn `workers` parallel armature subprocesses against the shared trace DB
    (HOME = sandbox dir). `armature_loop` runs `reps_per_worker` iterations in one
    process via --max-iterations; `armature_run_force` spawns `reps_per_worker`
    separate `armature run --force` processes per worker. Returns one summary dict
    per worker. Does NOT retry a BUSY crash — that is a finding to surface."""
    # Build a (worker_index, cmd) list. armature_loop: 1 cmd/worker (iterations via flag).
    # armature_run_force: reps_per_worker cmds/worker (one run each).
    cmds: list[tuple[int, list[str]]] = []
    for w in range(conc.workers):
        if conc.driver == "armature_loop":
            cmds.append((w, ["armature", "loop", str(spec_path),
                             "--max-iterations", str(conc.reps_per_worker), "--quiet"]))
        else:
            for _ in range(conc.reps_per_worker):
                cmds.append((w, ["armature", "run", str(spec_path), "--force", "--quiet"]))

    rows_before = _count_rows(sb.trace_db)
    procs = [subprocess.Popen(c, env=sb.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for (_w, c) in cmds]
    outs = [p.communicate() for p in procs]
    new_ids = _new_run_ids(sb.trace_db, rows_before)

    # group processes back into per-worker summaries
    summaries = []
    for w in range(conc.workers):
        idxs = [i for i, (wi, _c) in enumerate(cmds) if wi == w]
        w_procs = [procs[i] for i in idxs]
        w_outs = [outs[i] for i in idxs]
        exit_codes = [p.returncode for p in w_procs]
        busy = sum(o[1].lower().count("database is locked") + o[1].lower().count("sqlite_busy")
                   for o in w_outs)
        summaries.append({
            "run_id": None, "phase_id": phase_id, "lever": "none",
            "is_concurrency_summary": True, "worker": w,
            "exit_code": max(exit_codes) if exit_codes else 0,
            "exit_codes": exit_codes,
            "run_ids": [],           # filled below
            "n_trace_rows": 0,       # filled below
            "sqlite_busy_count": busy,
            "hqs_ours": None, "hqs_armature": None, "inputs": {},
            "improve_log": [], "recovery_hqs_ours": None,
            "spec_diff": "", "memory_mode": None,
        })

    # distribute new run_ids + new rows evenly across the worker summaries
    total_new = _count_rows(sb.trace_db) - rows_before
    per = total_new // len(summaries) if summaries else 0
    k = len(new_ids) // len(summaries) if summaries else 0
    # Capture each worker's trace rows so a recording can reproduce the
    # concurrency phase's trace DB footprint at replay time (kept off the
    # summary dict so it never leaks into campaign.jsonl).
    recorded_trace_rows: list[list[dict]] = []
    for i, s in enumerate(summaries):
        s["run_ids"] = new_ids[i * k:(i + 1) * k] if len(summaries) > 1 else new_ids
        s["n_trace_rows"] = per
        trs = []
        for rid in s["run_ids"]:
            trs.extend(asdict(r) for r in trace_io.read_rows_by_run(sb.trace_db, rid))
        # agents this worker spawned (LLM-stage rows across its run_ids) + the
        # workflow they belong to — feeds the per-workflow agent tally in reports.
        s["agents_run"] = trace_io.count_agent_spawns(trs)
        s["workflow_name"] = trs[0]["workflow_name"] if trs else ""
        recorded_trace_rows.append(trs)

    if recording is not None:
        for s, trs in zip(summaries, recorded_trace_rows):
            recording.record_run(None, ["armature", conc.driver, str(spec_path)], "", "",
                                 s["exit_code"], trs, {}, {}, tag="concurrency",
                                 meta={"summary": s})
    return summaries