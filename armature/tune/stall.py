"""Pure-function stall detector for the ``armature tune`` facade.

``detect_stall`` reads recent ``source="improve"`` records (newest-first, as
returned by ``ImprovementStore.load_history``) and decides whether the cheap
``improve`` engine is stuck — the signal the facade uses to escalate to the
expensive ``optimize`` engine. It is pure and synchronous so it is trivially
unit-testable and cheap to call every iteration.

A stall is a *pattern across cycles*, so a single record never qualifies
(``len(history) < 2`` → not stalled) — the facade never escalates on the first
improve run. Any one of five signals fires within the windowed history; the
strongest (by precedence) becomes the ``reason`` and every fired signal is
recorded in ``evidence`` for diagnostics.

Signals (precedence high → low):
- ``oscillation`` — latest record ``escalated_oscillation`` (drift-triggered,
  auto-apply suppressed): improve's own "I can't safely auto-resolve" signal.
- ``latency_cancel`` — ``latency_risk`` strictly rising across the latest two
  records while ``hqs_before`` is flat: the H4-v2 pattern (more fix-power,
  higher latency, net-zero HQS).
- ``repeated_missed`` — the same ``code:stage`` recurs in ``missed_predictions``
  across ≥2 records: the refiner's predicted fixes aren't materializing.
- ``regressions`` — non-empty ``unexpected_regressions`` across ≥2 records: each
  fix breaks something else.
- ``flat_hqs`` — the latest two "active" records (applied, or not applied but
  still missing predictions) have flat ``hqs_before`` (|Δ| < ε) below target:
  changes aren't moving the needle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from armature.state.improvement_store import ImprovementRecord


@dataclass
class StallVerdict:
    stalled: bool
    reason: str | None
    evidence: dict = field(default_factory=dict)


def _flat(h0: float | None, h1: float | None, epsilon: float) -> bool:
    """True when both HQS values are present and within ``epsilon`` of each other."""
    if h0 is None or h1 is None:
        return False
    return abs(h0 - h1) < epsilon


def _active(rec: ImprovementRecord) -> bool:
    """A record counts toward flat_hqs if improve actually tried something:
    applied a change, or failed to apply but still has outstanding misses."""
    return rec.applied or bool(rec.missed_predictions)


def detect_stall(
    history: list[ImprovementRecord],
    *,
    target_hqs: float,
    drift_threshold: float,
    hqs_epsilon: float = 0.01,
) -> StallVerdict:
    """Decide whether recent ``source="improve"`` history shows improve stalled.

    ``history`` is newest-first (as ``ImprovementStore.load_history`` returns).
    The caller slices to the desired window before calling — this function only
    sees what it is given.
    """
    if len(history) < 2:
        return StallVerdict(stalled=False, reason=None, evidence={})

    latest = history[0]
    prev = history[1]
    evidence: dict = {}
    reason: str | None = None

    # 1. oscillation — improve's own can't-safely-auto-resolve signal.
    if latest.escalated_oscillation:
        evidence["oscillation"] = True
        reason = "oscillation"

    # 2. latency_cancel — rising latency_risk while HQS is flat (H4-v2 pattern).
    if (
        latest.latency_risk is not None
        and prev.latency_risk is not None
        and latest.latency_risk > prev.latency_risk
        and _flat(latest.hqs_before, prev.hqs_before, hqs_epsilon)
    ):
        evidence["latency_cancel"] = True
        if reason is None:
            reason = "latency_cancel"

    # 3. repeated_missed — same code:stage in missed_predictions across >=2 records.
    missed_counts: dict[str, int] = {}
    for rec in history:
        for key in rec.missed_predictions:
            missed_counts[key] = missed_counts.get(key, 0) + 1
    if any(c >= 2 for c in missed_counts.values()):
        evidence["repeated_missed"] = True
        if reason is None:
            reason = "repeated_missed"

    # 4. regressions — unexpected_regressions across >=2 records.
    if sum(1 for rec in history if rec.unexpected_regressions) >= 2:
        evidence["regressions"] = True
        if reason is None:
            reason = "regressions"

    # 5. flat_hqs — latest two active records, HQS flat and below target.
    if (
        _active(latest)
        and _active(prev)
        and _flat(latest.hqs_before, prev.hqs_before, hqs_epsilon)
        and latest.hqs_before is not None
        and latest.hqs_before < target_hqs
    ):
        evidence["flat_hqs"] = True
        if reason is None:
            reason = "flat_hqs"

    return StallVerdict(stalled=reason is not None, reason=reason, evidence=evidence)