import pytest
from unittest.mock import patch
from armature.spec.models import Stage, HarnessSpec, ModelTiers, ModelTierConfig, Role, RoleType
from armature.runtime.engine import Harness


class _CreditsErr(Exception):
    status_code = 402

    def __init__(self):
        super().__init__("insufficient credits")


async def test_failed_stage_records_error_kind(tmp_path):
    spec = HarnessSpec(
        name="kind-test",
        stages=[Stage(
            id="doer",
            role=Role(name="Worker", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")

    async def mock_execute(context):
        raise _CreditsErr()

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        with pytest.raises(_CreditsErr):
            await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="kind-test")
    assert len(traces) == 1
    assert traces[0].success is False
    assert traces[0].error_type == "_CreditsErr"
    assert traces[0].error_kind == "provider_credits"


async def test_successful_stage_error_kind_none(tmp_path):
    spec = HarnessSpec(
        name="kind-ok",
        stages=[Stage(
            id="doer",
            role=Role(name="Worker", type=RoleType.WORKER, description="work", model_tier="small"),
        )],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "traces.db")

    async def mock_execute(context):
        return {"content": "done", "_input_tokens": 1, "_output_tokens": 1}

    with patch("armature.nodes.llm.LLMNode.execute", side_effect=mock_execute):
        await harness.run({})

    await harness._ensure_traces()
    traces = await harness._traces.query(workflow_name="kind-ok")
    assert len(traces) == 1
    assert traces[0].success is True
    assert traces[0].error_kind is None
