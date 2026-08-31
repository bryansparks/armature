import asyncio
from armature.state.traces import TraceRecord, TraceStore


def test_context_policy_round_trips(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    asyncio.run(store.init())
    rec = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="a",
        role_type="worker", model="m",
        context_policy={"must": ["mission", "principles"], "never": ["raw_pii"]},
    )
    asyncio.run(store.record(rec))
    rows = asyncio.run(store.query_by_run("r1"))
    assert len(rows) == 1
    assert rows[0].context_policy == {"must": ["mission", "principles"],
                                      "never": ["raw_pii"]}


def test_context_policy_null_when_unset(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    asyncio.run(store.init())
    rec = TraceRecord(run_id="r1", workflow_name="wf", stage_id="a",
                      role_type="worker", model="m")
    asyncio.run(store.record(rec))
    rows = asyncio.run(store.query_by_run("r1"))
    assert rows[0].context_policy is None


def test_init_is_idempotent_with_context_policy_column(tmp_path):
    db = tmp_path / "t.db"
    asyncio.run(TraceStore(db).init())
    asyncio.run(TraceStore(db).init())  # second init adds no duplicate column
