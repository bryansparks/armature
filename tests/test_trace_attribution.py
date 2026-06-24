from armature.state.traces import TraceRecord


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