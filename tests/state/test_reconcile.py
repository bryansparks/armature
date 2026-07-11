"""Reconciler classify() decision table + reconcile_batch() end-to-end."""
import pytest
from armature.state.knowledge import KnowledgeRecord, MemoryType
from armature.state.reconcile import classify, Reconciler, _union_provenance


def _rec(fact="prefers dark mode", entity="user", confidence=0.9, rtype=MemoryType.PREFERENCE, rid=None):
    return KnowledgeRecord(
        workflow_name="wf", entity=entity, fact=fact, confidence=confidence,
        source_run_id="r1", type=rtype, id=rid,
    )


# ── classify() decision table ─────────────────────────────────────────────────

def test_classify_store_when_no_neighbors():
    decision, target = classify(_rec("a brand new fact"), [])
    assert decision == "STORE" and target is None


def test_classify_store_when_low_similarity():
    neighbor = _rec("completely unrelated topic", rid=1)
    decision, target = classify(_rec("a brand new fact"), [neighbor])
    assert decision == "STORE"


def test_classify_skip_on_strict_duplicate_lower_confidence():
    neighbor = _rec("prefers dark mode", confidence=0.95, rid=1)
    decision, target = classify(_rec("prefers dark mode", confidence=0.9), [neighbor])
    assert decision == "SKIP" and target is neighbor


def test_classify_update_in_place_on_small_confidence_gain():
    neighbor = _rec("prefers dark mode", confidence=0.85, rid=1)
    decision, target = classify(_rec("prefers dark mode", confidence=0.9), [neighbor])
    assert decision == "UPDATE" and target is neighbor


def test_classify_supersede_on_large_confidence_gain_same_type():
    neighbor = _rec("prefers dark mode", confidence=0.5, rid=1)
    decision, target = classify(_rec("prefers dark mode", confidence=0.9), [neighbor])
    assert decision == "SUPERSEDE" and target is neighbor


def test_classify_merge_on_partial_overlap_same_type():
    neighbor = _rec("prefers dark mode sometimes", confidence=0.8, rid=1)
    decision, target = classify(_rec("prefers dark mode", confidence=0.9), [neighbor])
    assert decision == "MERGE" and target is neighbor


def test_union_provenance_dedupes_by_run_id():
    a = [{"run_id": "r1", "stage_id": "s1"}, {"run_id": "r2"}]
    b = [{"run_id": "r2", "stage_id": "s2"}, {"run_id": "r3"}]
    merged = _union_provenance(a, b)
    runs = sorted(m["run_id"] for m in merged)
    assert runs == ["r1", "r2", "r3"]
    # r2 entries are unioned (stage_id from whichever had it)
    assert any(m["run_id"] == "r2" for m in merged)


# ── reconcile_batch() ─────────────────────────────────────────────────────────

async def test_reconcile_batch_stores_new_and_skips_duplicate(tmp_path):
    from armature.state.knowledge import KnowledgeStore
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rec = Reconciler(store)
    await rec.reconcile_batch([_rec("prefers dark mode", confidence=0.9)])
    await rec.reconcile_batch([_rec("prefers dark mode", confidence=0.85)])  # SKIP
    loaded = await store.load("wf")
    assert len(loaded) == 1  # duplicate not stored


async def test_reconcile_batch_supersedes_on_higher_confidence(tmp_path):
    from armature.state.knowledge import KnowledgeStore
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rec = Reconciler(store)
    await rec.reconcile_batch([_rec("prefers dark mode", confidence=0.5)])
    await rec.reconcile_batch([_rec("prefers dark mode", confidence=0.95)])
    loaded = await store.load("wf")
    assert len(loaded) == 1
    assert loaded[0].confidence == 0.95


async def test_reconcile_batch_never_raises_on_bad_candidate(tmp_path):
    """A malformed candidate is swallowed, not propagated."""
    from armature.state.knowledge import KnowledgeStore
    store = KnowledgeStore(tmp_path / "k.db")
    await store.init()
    rec = Reconciler(store)
    bad = KnowledgeRecord(workflow_name="wf", entity="e", fact="", confidence=0.9, source_run_id="r1")
    await rec.reconcile_batch([bad])  # must not raise
    assert await store.load("wf") == []