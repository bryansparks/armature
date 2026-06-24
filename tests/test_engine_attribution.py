"""Integration test: LLM-stage trace records agent + skill attribution.

Exercises the real `Harness.run()` path in `armature/runtime/engine.py`,
stubbing only the LLM node so no network call is made. Asserts that the
trace recorded at the LLM-stage record site (lines ~440-464) carries
`agent_id`, `agent_version`, and `active_skill_ids` populated from the
resolved bundle role's extras and `stage.role.skills`.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from armature.runtime.engine import Harness
from armature.spec.models import HarnessSpec, Role, RoleType, Stage


def _make_role_with_agent_extra() -> Role:
    # A resolved bundle role: type worker, with x_source / x_agent_version
    # pydantic extras + 1 attached skill. Pydantic v2 `extra="allow"` stores
    # these as attributes accessible via getattr, mirroring how Cabinet's
    # compiler emits an agent.yaml role and how spec/loader.py copies
    # `bundle.role` onto `stage.role`.
    return Role(
        name="Gmail Reader",
        type=RoleType.WORKER,
        description="Read and triage the inbox.",
        skills=["triage-inbox"],
        x_source="gmail-reader",
        x_agent_version="0.2.0",
    )


async def test_llm_stage_records_agent_attribution(monkeypatch, tmp_path: Path):
    # Minimal spec: one LLM stage whose role carries agent extra + a skill.
    stage = Stage(
        id="s1",
        role=_make_role_with_agent_extra(),
        output_mode="text",
        depends_on=[],
    )
    spec = HarnessSpec(name="wf", version="1.0", stages=[stage])

    # Stub the LLM node so execute() returns a plain dict result — no network.
    fake_node = MagicMock()
    fake_node._resolve_model.return_value = "m"
    fake_result = {
        "content": "ok",
        "_input_tokens": 1,
        "_output_tokens": 1,
        "_tools_declared": [],
        "_tools_called": [],
        "_escalation_count": 0,
    }
    fake_node.execute = AsyncMock(return_value=fake_result)
    # LLMNode is constructed with many kwargs inside _execute_stage; absorb all.
    monkeypatch.setattr("armature.runtime.engine.LLMNode", lambda **kw: fake_node)

    engine = Harness(
        spec,
        traces_db=tmp_path / "traces.db",
        use_cache=False,
        validate=False,
    )

    await engine.run()

    rows = await engine._traces.query(workflow_name="wf")
    assert rows, "a trace should have been recorded for the LLM stage"
    t = rows[0]
    assert t.agent_id == "gmail-reader"
    assert t.agent_version == "0.2.0"
    assert t.active_skill_ids == ["triage-inbox"]


async def test_llm_stage_records_null_attribution_for_inline_role(monkeypatch, tmp_path: Path):
    """An inline `role:` stage (no bundle) records None / [] attribution."""
    stage = Stage(
        id="s1",
        role=Role(name="Worker", type=RoleType.WORKER, description="d"),
        output_mode="text",
        depends_on=[],
    )
    spec = HarnessSpec(name="wf2", version="1.0", stages=[stage])

    fake_node = MagicMock()
    fake_node._resolve_model.return_value = "m"
    fake_result = {
        "content": "ok",
        "_input_tokens": 1,
        "_output_tokens": 1,
        "_tools_declared": [],
        "_tools_called": [],
        "_escalation_count": 0,
    }
    fake_node.execute = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("armature.runtime.engine.LLMNode", lambda **kw: fake_node)

    engine = Harness(
        spec,
        traces_db=tmp_path / "traces2.db",
        use_cache=False,
        validate=False,
    )
    await engine.run()

    rows = await engine._traces.query(workflow_name="wf2")
    assert rows, "a trace should have been recorded"
    t = rows[0]
    assert t.agent_id is None
    assert t.agent_version is None
    assert t.active_skill_ids == []