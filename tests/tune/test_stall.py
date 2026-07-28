"""Tests for the pure-function stall detector (``armature.tune.stall``).

``detect_stall`` reads recent ``source="improve"`` records (newest-first, as
returned by ``ImprovementStore.load_history``) and decides whether the cheap
engine is stuck — the signal the ``tune`` facade uses to escalate to the
expensive ``optimize`` engine. It is pure + synchronous.
"""
import pytest

from armature.state.improvement_store import ImprovementRecord
from armature.tune.stall import StallVerdict, detect_stall


def _rec(
    *,
    record_id="r0",
    escalated_oscillation=False,
    triggered_by_drift=False,
    applied=False,
    hqs_before=None,
    drift_score=0.0,
    latency_risk=0.0,
    missed_predictions=None,
    verified_fixes=None,
    unexpected_regressions=None,
    timestamp=None,
):
    from datetime import datetime, timezone
    return ImprovementRecord(
        record_id=record_id,
        workflow_stem="wf",
        source="improve",
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        escalated_oscillation=escalated_oscillation,
        triggered_by_drift=triggered_by_drift,
        applied=applied,
        hqs_before=hqs_before,
        drift_score=drift_score,
        latency_risk=latency_risk,
        missed_predictions=missed_predictions or [],
        verified_fixes=verified_fixes or [],
        unexpected_regressions=unexpected_regressions or [],
    )


def _detect(history, *, target_hqs=0.90, drift_threshold=0.5, hqs_epsilon=0.01):
    return detect_stall(
        history,
        target_hqs=target_hqs,
        drift_threshold=drift_threshold,
        hqs_epsilon=hqs_epsilon,
    )


def test_not_enough_history_is_not_stalled():
    """A single cycle is not a stall — don't escalate on the first improve run."""
    assert _detect([_rec(hqs_before=0.4)]).stalled is False
    assert _detect([]).stalled is False


def test_oscillation_signal_stalls():
    """Latest record escalated_oscillation (drift-triggered, auto-apply suppressed)
    across >=2 cycles means improve can't safely auto-resolve → escalate."""
    history = [
        _rec(record_id="r2", escalated_oscillation=True, triggered_by_drift=True, hqs_before=0.91),
        _rec(record_id="r1", escalated_oscillation=True, triggered_by_drift=True, hqs_before=0.90),
    ]
    v = _detect(history, target_hqs=0.85)
    assert v.stalled is True
    assert v.reason == "oscillation"


def test_repeated_missed_predictions_stall():
    """The same code:stage recurring in missed_predictions across >=2 cycles
    means the refiner's fixes aren't materializing → escalate."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.60,
             missed_predictions=["output_invalid:analyst", "stage_failed:writer"]),
        _rec(record_id="r1", applied=True, hqs_before=0.58,
             missed_predictions=["output_invalid:analyst"]),
    ]
    v = _detect(history, target_hqs=0.90)
    assert v.stalled is True
    assert v.reason == "repeated_missed"


def test_repeated_missed_requires_same_key_across_cycles():
    """Different missed keys each cycle is not a repeated-miss stall."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.60, missed_predictions=["a:x"]),
        _rec(record_id="r1", applied=True, hqs_before=0.58, missed_predictions=["b:y"]),
    ]
    assert _detect(history, target_hqs=0.90).stalled is False


def test_flat_hqs_stall():
    """applied=True with hqs_before flat (|Δ|<ε) and below target across >=2
    cycles means changes aren't moving the needle → escalate."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.600),
        _rec(record_id="r1", applied=True, hqs_before=0.605),
    ]
    v = _detect(history, target_hqs=0.90, hqs_epsilon=0.01)
    assert v.stalled is True
    assert v.reason == "flat_hqs"


def test_flat_hqs_not_stalled_when_rising():
    """hqs_before climbing toward target is progress, not a stall."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.70),
        _rec(record_id="r1", applied=True, hqs_before=0.55),
    ]
    assert _detect(history, target_hqs=0.90).stalled is False


def test_flat_hqs_not_stalled_when_above_target():
    """Flat but already at/above target is converged, not stalled."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.905),
        _rec(record_id="r1", applied=True, hqs_before=0.900),
    ]
    assert _detect(history, target_hqs=0.90).stalled is False


def test_latency_cancel_stall():
    """latency_risk strictly rising while hqs_before flat = the H4-v2 pattern
    (more fix-power, higher latency, net-zero HQS) → escalate."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.600, latency_risk=2.0),
        _rec(record_id="r1", applied=True, hqs_before=0.602, latency_risk=1.0),
    ]
    v = _detect(history, target_hqs=0.90, hqs_epsilon=0.01)
    assert v.stalled is True
    assert v.reason == "latency_cancel"


def test_regressions_stall():
    """non-empty unexpected_regressions across >=2 cycles = each fix breaks
    something else → escalate."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.60, unexpected_regressions=["low_confidence:judge"]),
        _rec(record_id="r1", applied=True, hqs_before=0.58, unexpected_regressions=["low_confidence:judge"]),
    ]
    v = _detect(history, target_hqs=0.90)
    assert v.stalled is True
    assert v.reason == "regressions"


def test_healthy_history_is_not_stalled():
    """applied=True, hqs climbing, no regressions, no repeated misses → healthy."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.82, verified_fixes=["output_invalid:analyst"]),
        _rec(record_id="r1", applied=True, hqs_before=0.70, verified_fixes=["stage_failed:writer"]),
    ]
    assert _detect(history, target_hqs=0.90).stalled is False


def test_window_slicing_only_uses_recent_records():
    """The caller slices to the window; detect_stall only sees what it's given.
    A stall in the windowed-in recent records is detected even if older records
    (outside the window) were healthy — and vice versa."""
    # Two stalled recent records → stalled.
    recent = [
        _rec(record_id="r2", applied=True, hqs_before=0.60, missed_predictions=["a:x"]),
        _rec(record_id="r1", applied=True, hqs_before=0.58, missed_predictions=["a:x"]),
    ]
    assert _detect(recent, target_hqs=0.90).stalled is True


def test_reason_precedence_oscillation_wins():
    """When multiple signals fire, oscillation (improve's own can't-resolve
    signal) is the strongest reason."""
    history = [
        _rec(record_id="r2", escalated_oscillation=True, triggered_by_drift=True,
             applied=False, hqs_before=0.60, latency_risk=2.0,
             missed_predictions=["a:x"], unexpected_regressions=["b:y"]),
        _rec(record_id="r1", escalated_oscillation=True, triggered_by_drift=True,
             applied=False, hqs_before=0.58, latency_risk=1.0,
             missed_predictions=["a:x"], unexpected_regressions=["b:y"]),
    ]
    v = _detect(history, target_hqs=0.90)
    assert v.stalled is True
    assert v.reason == "oscillation"
    # evidence records all fired signals
    assert "oscillation" in v.evidence
    assert v.evidence["oscillation"] is True


def test_evidence_records_fired_signals():
    """The evidence dict lists every signal that fired, for diagnostics."""
    history = [
        _rec(record_id="r2", applied=True, hqs_before=0.600, latency_risk=2.0,
             missed_predictions=["a:x"], unexpected_regressions=["b:y"]),
        _rec(record_id="r1", applied=True, hqs_before=0.602, latency_risk=1.0,
             missed_predictions=["a:x"], unexpected_regressions=["b:y"]),
    ]
    v = _detect(history, target_hqs=0.90, hqs_epsilon=0.01)
    assert v.stalled is True
    # latency_cancel is the strongest here (precedence over repeated_missed/regressions/flat)
    assert v.reason == "latency_cancel"
    assert v.evidence.get("latency_cancel") is True
    assert v.evidence.get("repeated_missed") is True
    assert v.evidence.get("regressions") is True