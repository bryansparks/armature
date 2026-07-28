"""Tests for the unified ImprovementStore shared by improve + optimize."""
import pytest
from armature.state.improvement_store import ImprovementRecord, ImprovementStore


@pytest.fixture
async def store(tmp_path):
    s = ImprovementStore(tmp_path / "improvements.db")
    await s.init()
    return s


def _rec(*, record_id="r0", workflow_stem="my-flow", source="optimize",
          accepted=True, score=0.88, rationale="Fix parse errors",
          proposed_diff="- foo\n+ bar", confidence=0.85, feedback="Good change",
          verified_fixes=None, missed_predictions=None):
    return ImprovementRecord(
        record_id=record_id, workflow_stem=workflow_stem, source=source,
        proposed_diff=proposed_diff, rationale=rationale, confidence=confidence,
        accepted=accepted, score=score, feedback=feedback,
        verified_fixes=verified_fixes or [], missed_predictions=missed_predictions or [],
    )


async def test_init_is_idempotent(tmp_path):
    store = ImprovementStore(tmp_path / "improvements.db")
    await store.init()
    await store.init()  # must not raise


async def test_record_and_load(store):
    await store.record(_rec(record_id="abc12345"))
    history = await store.load_history("my-flow")
    assert len(history) == 1
    assert history[0].record_id == "abc12345"
    assert history[0].source == "optimize"
    assert history[0].accepted is True
    assert history[0].confidence == pytest.approx(0.85)
    assert history[0].timestamp  # default_factory populated a non-empty ISO timestamp


async def test_load_history_filters_by_workflow_stem(store):
    await store.record(_rec(record_id="r1", workflow_stem="flow-a"))
    await store.record(_rec(record_id="r2", workflow_stem="flow-b", accepted=False, score=0.3))
    a = await store.load_history("flow-a")
    b = await store.load_history("flow-b")
    assert len(a) == 1 and a[0].record_id == "r1"
    assert len(b) == 1 and b[0].record_id == "r2"


async def test_load_history_filters_by_source(store):
    """Each engine reads the other's records via the source filter."""
    await store.record(_rec(record_id="opt1", workflow_stem="wf", source="optimize"))
    await store.record(_rec(record_id="imp1", workflow_stem="wf", source="improve",
                            accepted=True, score=0.4,
                            verified_fixes=["output_invalid:analyst"],
                            missed_predictions=["stage_failed:writer"]))
    opt_only = await store.load_history("wf", source="optimize")
    imp_only = await store.load_history("wf", source="improve")
    assert len(opt_only) == 1 and opt_only[0].record_id == "opt1"
    assert len(imp_only) == 1 and imp_only[0].record_id == "imp1"
    # improve-side fields round-trip through the store
    assert imp_only[0].verified_fixes == ["output_invalid:analyst"]
    assert imp_only[0].missed_predictions == ["stage_failed:writer"]


async def test_load_history_returns_most_recent_first(store):
    for i in range(5):
        await store.record(_rec(record_id=f"r{i}", workflow_stem="wf"))
    history = await store.load_history("wf", limit=3)
    assert len(history) == 3
    assert history[0].record_id == "r4"  # most recent first


async def test_empty_db_returns_empty_history(store):
    assert await store.load_history("nonexistent") == []
    assert await store.load_history("nonexistent", source="improve") == []


async def test_improve_record_round_trips_empty_optimize_fields(store):
    """An improve-source record preserves its verification fields and leaves
    optimize-side fields at their defaults."""
    await store.record(ImprovementRecord(
        record_id="imp9", workflow_stem="wf", source="improve",
        predicted_fixes=["output_invalid:analyst"], predicted_regressions=["low_confidence:judge"],
        verified_fixes=["output_invalid:analyst"], missed_predictions=[],
        unexpected_regressions=[], applied=True, hqs_before=0.42,
        drift_score=0.6, triggered_by_drift=True, escalated_oscillation=True,
        latency_risk=1.5,
    ))
    [rec] = await store.load_history("wf", source="improve")
    assert rec.predicted_fixes == ["output_invalid:analyst"]
    assert rec.predicted_regressions == ["low_confidence:judge"]
    assert rec.applied is True
    assert rec.hqs_before == pytest.approx(0.42)
    assert rec.drift_score == pytest.approx(0.6)
    assert rec.triggered_by_drift is True
    assert rec.escalated_oscillation is True
    assert rec.latency_risk == pytest.approx(1.5)
    # optimize-side fields default
    assert rec.proposed_diff == ""
    assert rec.rationale == ""
    assert rec.confidence == 0.0