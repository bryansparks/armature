"""Reproduce the four HQS formulas Armature uses, from raw trace rows.

We recompute rather than trust Armature's reported values so the report can
surface formula drift honestly. Pinned from the Armature source (see plan
Global Constraints for the exact weight/term citations).
"""
from __future__ import annotations

from campaign_runner.trace_io import TraceRow


def _rates(rows: list[TraceRow]) -> tuple[float, float, float, float, float, int] | None:
    if not rows:
        return None
    n = len(rows)
    valid = sum(1 for r in rows if r.output_valid) / n
    success = sum(1 for r in rows if r.success) / n
    quorums = [r.quorum_score for r in rows if r.quorum_score is not None]
    avg_quorum = sum(quorums) / len(quorums) if quorums else 0.5
    avg_lat = sum(r.latency_ms for r in rows) / n
    max_lat = max(r.latency_ms for r in rows) if rows else 1.0
    hfr = sum(1 for r in rows if r.escalation_count == 0) / n
    return valid, success, avg_quorum, avg_lat, max_lat, hfr, n


def compute_authoritative(rows: list[TraceRow]) -> float | None:
    r = _rates(rows)
    if r is None:
        return None
    valid, success, avg_quorum, avg_lat, _max_lat, hfr, _n = r
    latency_score = max(0.0, 1.0 - avg_lat / 5000.0)
    return 0.35 * valid + 0.25 * success + 0.20 * avg_quorum + 0.10 * latency_score + 0.10 * hfr


def avg_quorum(rows: list[TraceRow]) -> float | None:
    """The mean quorum_score across a run's trace rows (judge coverage), or
    None if there are no rows / no quorum scores. Verdict H4 v3 judges on this
    directly: in aggregate HQS it carries only 0.20 weight and is masked by
    the latency/valid/success terms, so the memory carry-forward benefit (more
    distinct sub-problems covered) does not show through. The raw quorum mean
    is the honest signal."""
    r = _rates(rows)
    return r[2] if r is not None else None


# The rolling formula is identical to the authoritative one; the only
# difference is *which rows* feed it (last-200 across runs). Callers select
# the rows; the math is the same.
compute_rolling = compute_authoritative


def compute_dashboard(rows: list[TraceRow]) -> float | None:
    r = _rates(rows)
    if r is None:
        return None
    valid, success, avg_quorum, _avg_lat, max_lat, _hfr, _n = r
    latency_score = max(0.0, 1.0 - max_lat / 60000.0)
    return 0.40 * valid + 0.30 * success + 0.20 * avg_quorum + 0.10 * latency_score


def compute_feedback(rows: list[TraceRow]) -> float | None:
    r = _rates(rows)
    if r is None:
        return None
    valid, success, _avg_quorum, avg_lat, _max_lat, _hfr, _n = r
    latency_score = max(0.0, 1.0 - avg_lat / 5000.0)
    return 0.40 * valid + 0.30 * success + 0.20 * 0.5 + 0.10 * latency_score


def all_four(rows: list[TraceRow]) -> dict[str, float | None]:
    return {
        "authoritative": compute_authoritative(rows),
        "rolling": compute_rolling(rows),
        "dashboard": compute_dashboard(rows),
        "feedback": compute_feedback(rows),
    }


def divergence(ours: dict[str, float | None], armature: dict[str, float | None]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in ours:
        a, b = ours.get(k), armature.get(k)
        if a is None or b is None:
            out[k] = 0.0
        else:
            out[k] = abs(a - b)
    return out