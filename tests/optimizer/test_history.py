import pytest
from pathlib import Path
from armature.optimizer.history import ProposalRecord, ProposalStore


@pytest.fixture
async def store(tmp_path):
    s = ProposalStore(tmp_path / "proposals.db")
    await s.init()
    return s


async def test_init_is_idempotent(tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    await store.init()
    await store.init()   # must not raise


async def test_record_and_load(store):
    rec = ProposalRecord(
        proposal_id="abc12345",
        workflow_name="my-flow",
        proposed_diff="- foo\n+ bar",
        rationale="Fix JSON parse error",
        confidence=0.85,
        accepted=True,
        score=0.88,
        feedback="Good change",
    )
    await store.record(rec)
    history = await store.load_history("my-flow")
    assert len(history) == 1
    assert history[0].proposal_id == "abc12345"
    assert history[0].accepted is True
    assert history[0].confidence == pytest.approx(0.85)


async def test_load_history_filters_by_workflow(store):
    await store.record(ProposalRecord(
        proposal_id="r1", workflow_name="flow-a",
        proposed_diff="diff1", rationale="r", confidence=0.8,
        accepted=True, score=0.8, feedback="ok",
    ))
    await store.record(ProposalRecord(
        proposal_id="r2", workflow_name="flow-b",
        proposed_diff="diff2", rationale="r", confidence=0.7,
        accepted=False, score=0.3, feedback="bad",
    ))
    a = await store.load_history("flow-a")
    b = await store.load_history("flow-b")
    assert len(a) == 1 and a[0].proposal_id == "r1"
    assert len(b) == 1 and b[0].proposal_id == "r2"


async def test_load_history_returns_most_recent_first(store):
    for i in range(5):
        await store.record(ProposalRecord(
            proposal_id=f"r{i}", workflow_name="wf",
            proposed_diff=f"diff{i}", rationale="r", confidence=0.7,
            accepted=bool(i % 2), score=0.7, feedback="ok",
        ))
    history = await store.load_history("wf", limit=3)
    assert len(history) == 3
    assert history[0].proposal_id == "r4"  # most recent first


async def test_empty_db_returns_empty_history(store):
    history = await store.load_history("nonexistent")
    assert history == []
