import asyncio
from armature.state.traces import TraceStore, TraceRecord


def test_error_kind_roundtrips(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    asyncio.run(store.init())
    rec = TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s", role_type="judge",
        model="m", success=False, output_valid=False,
        error_type="BadRequestError", error_kind="provider_credits",
    )
    asyncio.run(store.record(rec))
    rows = asyncio.run(store.query_by_run("r1"))
    assert len(rows) == 1
    assert rows[0].error_kind == "provider_credits"
    assert rows[0].error_type == "BadRequestError"


def test_success_error_kind_defaults_none(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    asyncio.run(store.init())
    rec = TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s", role_type="worker",
        model="m", success=True, output_valid=True, quorum_score=0.8,
    )
    asyncio.run(store.record(rec))
    rows = asyncio.run(store.query_by_run("r1"))
    assert rows[0].error_kind is None


def test_init_is_idempotent_with_new_column(tmp_path):
    """A DB created before error_kind existed must get the column via ALTER on
    a later init(), without error."""
    db = tmp_path / "t.db"
    asyncio.run(TraceStore(db).init())
    asyncio.run(TraceStore(db).init())  # second init adds no duplicate column
    store = TraceStore(db)
    asyncio.run(store.init())
    rec = TraceRecord(run_id="r1", workflow_name="w", stage_id="s",
                      role_type="worker", model="m", error_kind="provider_auth")
    asyncio.run(store.record(rec))
    assert asyncio.run(store.query_by_run("r1"))[0].error_kind == "provider_auth"