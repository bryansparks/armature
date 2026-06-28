"""Reliability verdicts for the soak test. Each returns (name, status, detail).

Statuses: PASS / FAIL / INCONCLUSIVE. INCONCLUSIVE means the run did not
exercise the signal (e.g. no concurrency phase) — an observability note,
not a quiet pass.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from campaign_runner.trace_io import LLM_ROLE_TYPES

PASS, FAIL, INCON = "PASS", "FAIL", "INCONCLUSIVE"


def _soak_rows(rows):
    return [r for r in rows if not r.get("is_concurrency_summary")]


def verdict_no_unclean_exits(rows, th):
    sr = _soak_rows(rows)
    allowed = th.get("allowed_failures", 0)
    bad = [r for r in sr if r.get("exit_code") not in (0, None)]
    ok = len(bad) <= allowed
    return ("no_unclean_exits", PASS if ok else FAIL,
            {"n_runs": len(sr), "n_unclean": len(bad),
             "allowed_failures": allowed,
             "bad_run_ids": [r.get("run_id") for r in bad]})


def verdict_trace_db_integrity(rows, th, trace_db):
    if not trace_db or not Path(trace_db).exists():
        return ("trace_db_integrity", INCON, {"n_rows": 0, "note": "no trace db"})
    try:
        con = sqlite3.connect(str(trace_db))
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        n_null = con.execute("SELECT count(*) FROM traces WHERE run_id IS NULL").fetchone()[0]
        n_rows = con.execute("SELECT count(*) FROM traces").fetchone()[0]
        con.close()
    except Exception as e:
        return ("trace_db_integrity", FAIL, {"error": str(e)})
    allow_null = th.get("allow_null_run_id", 0)
    ok = integrity == "ok" and n_null <= allow_null
    return ("trace_db_integrity", PASS if ok else FAIL,
            {"integrity_check": integrity, "n_null_run_id": n_null, "n_rows": n_rows})


def verdict_no_row_loss_under_concurrency(rows, th):
    conc = [r for r in rows if r.get("is_concurrency_summary")]
    expected = th.get("expected", 0)
    if not conc:
        return ("no_row_loss_under_concurrency", INCON,
                {"n_concurrency_workers": 0, "expected": expected})
    tol = th.get("tolerance", 0)
    all_ids = [rid for r in conc for rid in (r.get("run_ids") or [])]
    actual = len(set(all_ids))
    busy = sum(r.get("sqlite_busy_count", 0) for r in conc)
    all_exit0 = all(r.get("exit_codes") and all(c == 0 for c in r["exit_codes"]) for r in conc)
    ok = busy == 0 and all_exit0 and abs(actual - expected) <= tol
    return ("no_row_loss_under_concurrency", PASS if ok else FAIL,
            {"expected": expected, "actual": actual, "sqlite_busy_count": busy,
             "per_worker_rows": [r.get("n_trace_rows") for r in conc]})


def verdict_hqs_stability_no_drift(rows, th):
    sr = _soak_rows(rows)
    vals = [(r.get("hqs_ours") or {}).get("authoritative") for r in sr]
    vals = [v for v in vals if v is not None]
    if len(vals) < 8:
        return ("hqs_stability_no_drift", INCON, {"n": len(vals)})
    n = len(vals)
    q = max(1, n // 4)
    q1, q4 = vals[:q], vals[-q:]
    m1, m4 = sum(q1) / len(q1), sum(q4) / len(q4)
    delta = abs(m1 - m4)
    ok = delta <= th.get("max_mean_delta", 0.08)
    return ("hqs_stability_no_drift", PASS if ok else FAIL,
            {"q1_mean": round(m1, 4), "q4_mean": round(m4, 4),
             "delta": round(delta, 4), "n": n})


def verdict_wallclock_stability(rows, th, trace_db):
    sr = _soak_rows(rows)
    ids = [r.get("run_id") for r in sr if r.get("run_id")]
    if len(ids) < 8 or not trace_db or not Path(trace_db).exists():
        return ("wallclock_stability", INCON, {"n": len(ids)})
    try:
        con = sqlite3.connect(str(trace_db))
        means = []
        for rid in ids:
            v = con.execute("SELECT avg(latency_ms) FROM traces WHERE run_id=?", (rid,)).fetchone()[0]
            means.append(v or 0.0)
        con.close()
    except Exception as e:
        return ("wallclock_stability", FAIL, {"error": str(e)})
    n = len(means)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(means) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, means))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den if den else 0.0
    ok = slope <= th.get("max_latency_slope_ms_per_run", 5.0)
    return ("wallclock_stability", PASS if ok else FAIL,
            {"slope_ms_per_run": round(slope, 4), "n": n})


def verdict_checkpoint_resume_correctness(rows, th):
    sr = _soak_rows(rows)
    ids = [r.get("run_id") for r in sr if r.get("run_id")]
    if not ids:
        return ("checkpoint_resume_correctness", INCON, {"n_runs": 0})
    distinct = len(set(ids))
    dups = sorted({rid for rid in ids if ids.count(rid) > 1})
    require_distinct = th.get("require_distinct_run_ids", True)
    ok = (not require_distinct) or (distinct == len(ids))
    return ("checkpoint_resume_correctness", PASS if ok else FAIL,
            {"n_runs": len(ids), "n_distinct_run_ids": distinct, "dup_run_ids": dups})


def verdict_budget_obeyed(rows, th, plan):
    sr = _soak_rows(rows)
    mr = plan.budget.max_runs
    ok = len(sr) <= mr
    stop = "budget" if len(sr) >= mr else "completed"
    return ("budget_obeyed", PASS if ok else FAIL,
            {"n_rows": len(sr), "max_runs": mr, "stop_reason": stop})


def verdict_agent_spawn_count(rows, th, trace_db):
    if not trace_db or not Path(trace_db).exists():
        return ("agent_spawn_count", INCON, {"total_agents": 0})
    try:
        con = sqlite3.connect(str(trace_db))
        qmarks = ",".join(f"'{t}'" for t in LLM_ROLE_TYPES)
        total = con.execute(
            f"SELECT count(*) FROM traces WHERE role_type IN ({qmarks})").fetchone()[0]
        con.close()
    except Exception as e:
        return ("agent_spawn_count", FAIL, {"error": str(e)})
    mn = th.get("min_total", 5000)
    ok = total >= mn
    return ("agent_spawn_count", PASS if ok else FAIL,
            {"total_agents": total, "min_total": mn})


def all_soak_verdicts(rows, plan, trace_db=None):
    if plan.soak_verdicts is None:
        return []
    sv = plan.soak_verdicts
    expected = 0
    for ph in plan.phases:
        if ph.concurrency is not None:
            expected = ph.concurrency.workers * ph.concurrency.reps_per_worker
            break
    th_nrl = dict(sv.no_row_loss_under_concurrency)
    th_nrl.setdefault("expected", expected)
    return [
        verdict_no_unclean_exits(rows, sv.no_unclean_exits),
        verdict_trace_db_integrity(rows, sv.trace_db_integrity, trace_db),
        verdict_no_row_loss_under_concurrency(rows, th_nrl),
        verdict_hqs_stability_no_drift(rows, sv.hqs_stability_no_drift),
        verdict_wallclock_stability(rows, sv.wallclock_stability, trace_db),
        verdict_checkpoint_resume_correctness(rows, sv.checkpoint_resume_correctness),
        verdict_budget_obeyed(rows, sv.budget_obeyed, plan),
        verdict_agent_spawn_count(rows, sv.agent_spawn_count, trace_db),
    ]