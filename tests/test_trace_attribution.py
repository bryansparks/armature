from pathlib import Path
from armature.state.traces import TraceStore, TraceRecord


def test_trace_record_has_attribution_fields_with_defaults():
    t = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="m",
    )
    assert t.agent_id is None
    assert t.agent_version is None
    assert t.active_skill_ids == []


def test_trace_record_accepts_attribution_values():
    t = TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="m",
        agent_id="gmail-reader", agent_version="0.2.0",
        active_skill_ids=["triage-inbox", "draft-reply"],
    )
    assert t.agent_id == "gmail-reader"
    assert t.agent_version == "0.2.0"
    assert t.active_skill_ids == ["triage-inbox", "draft-reply"]


async def test_store_round_trips_attribution(tmp_path: Path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    await store.record(TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="m",
        agent_id="gmail-reader", agent_version="0.2.0",
        active_skill_ids=["triage-inbox", "draft-reply"],
    ))
    rows = await store.query(workflow_name="wf")
    assert len(rows) == 1
    t = rows[0]
    assert t.agent_id == "gmail-reader"
    assert t.agent_version == "0.2.0"
    assert t.active_skill_ids == ["triage-inbox", "draft-reply"]


async def test_store_round_trips_null_attribution(tmp_path: Path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    await store.record(TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="m",
    ))  # inline role: stage -> no attribution
    rows = await store.query(workflow_name="wf")
    assert rows[0].agent_id is None
    assert rows[0].agent_version is None
    assert rows[0].active_skill_ids == []


async def test_legacy_record_call_still_works(tmp_path: Path):
    """A TraceRecord built without the new fields still records and reads back."""
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    await store.record(TraceRecord(
        run_id="r1", workflow_name="wf", stage_id="s1",
        role_type="worker", model="m",
        # no agent_id / agent_version / active_skill_ids
    ))
    rows = await store.query(workflow_name="wf")
    assert rows[0].agent_id is None
    assert rows[0].active_skill_ids == []