"""Reproduce the four HQS formulas Armature uses, from raw trace rows.

We recompute rather than trust Armature's reported values so the report can
surface formula drift honestly. Pinned from the Armature source (see plan
Global Constraints for the exact weight/term citations).
"""
from __future__ import annotations

import json

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


def is_model_failed(rows: list[TraceRow]) -> bool:
    """Detect degenerate model failures from a run's trace rows.

    A run is ``model_failed`` if EITHER:
      1. **Self-contradictory judge:** a judge row whose parsed ``outputs_json``
         has ``accept`` truthy/True AND ``confidence`` < 0.5. The judge prompt
         says "accept true iff confidence >= 0.5 and no fabrication";
         accept=True with confidence<0.5 is a self-contradiction = judge model
         failure (e.g. qwen3.6-27b guided_json emitting ``{"accept":"True",
         "confidence":"0"}`` on a GOOD briefing).
      2. **Empty researcher:** a researcher row whose parsed ``outputs_json``
         ``content`` (or ``text``) is empty or shorter than 40 chars. A real
         briefing is 200+ chars; the observed empty case was ~15.

    Coerces the stringified values the model emits defensively: ``accept`` may
    be ``"True"``/``"False"`` or bool; ``confidence`` may be ``"0"``/``"0.05"``
    or a number. A confidence that fails to parse as float does NOT by itself
    trigger the rule (treated as None). ``outputs_json`` that is not valid JSON
    makes that row contribute to neither condition (never crashes).

    Replay-deterministic: computed from trace rows alone, so ``_row_from_run``
    and ``replay()`` produce the same flag from the same recorded rows.
    """
    for r in rows:
        if r.role_type == "judge":
            try:
                out = json.loads(r.outputs_json or "{}")
            except Exception:
                continue
            if not isinstance(out, dict):
                continue
            acc = out.get("accept")
            conf = out.get("confidence")
            if acc is None or conf is None:
                continue
            accept_true = str(acc).strip().lower() in ("true", "1")
            try:
                confidence = float(conf)
            except (TypeError, ValueError):
                continue  # unparseable confidence does NOT trigger the rule
            if accept_true and confidence < 0.5:
                return True
        elif r.role_type == "researcher":
            try:
                out = json.loads(r.outputs_json or "{}")
            except Exception:
                continue
            if not isinstance(out, dict):
                continue
            content = out.get("content")
            if content is None:
                content = out.get("text")
            if content is None:
                continue
            if len(str(content).strip()) < 40:
                return True
    return False


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