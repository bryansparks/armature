import asyncio

from armature.state.traces import TraceRecord, TraceStore


def test_server_run_id_round_trips_and_defaults_to_none(tmp_path):
    db = tmp_path / "t.db"
    store = TraceStore(db)
    asyncio.run(store.init())
    asyncio.run(TraceStore(db).init())
    asyncio.run(store.record(TraceRecord(run_id="r1", workflow_name="wf", stage_id="a", role_type="worker", model="m",
                                         server_run_id="srv-1")))
    asyncio.run(store.record(TraceRecord(run_id="r2", workflow_name="wf", stage_id="a", role_type="worker",
                                         model="m")))
    assert [t.server_run_id for t in asyncio.run(store.query_by_run("r1"))] == ["srv-1"]
    assert [t.server_run_id for t in asyncio.run(store.query_by_run("r2"))] == [None]
