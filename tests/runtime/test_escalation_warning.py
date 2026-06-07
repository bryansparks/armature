"""Tests for tier_escalation_warning event emitted by the engine."""
import pytest
from unittest.mock import AsyncMock, patch
from armature.runtime.engine import Harness
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode,
)


def _spec_with_guided_json_small() -> HarnessSpec:
    return HarnessSpec(
        name="wf",
        model_tiers=ModelTiers(
            small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
            medium=ModelTierConfig(provider="openai", model="gpt-4o"),
        ),
        stages=[
            Stage(
                id="s",
                role=Role(name="R", type=RoleType.WORKER, description="d", model_tier="small"),
                output_mode=OutputMode.GUIDED_JSON,
                output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
                depends_on=[],
            )
        ],
    )


@pytest.mark.asyncio
async def test_tier_escalation_emits_warning_event(tmp_path):
    """Engine emits tier_escalation_warning when guided_json stage escalates tiers."""
    events: list[tuple[str, dict]] = []

    def capture(name, data):
        events.append((name, data))

    spec = _spec_with_guided_json_small()
    harness = Harness(spec=spec, session_dir=tmp_path, on_event=capture, validate=False, use_cache=False)

    # Patch LLMNode.execute to return a result with escalation_count=1
    with patch("armature.runtime.engine.LLMNode") as MockLLMNode:
        mock_instance = MockLLMNode.return_value
        mock_instance.execute = AsyncMock(return_value={
            "x": "hello",
            "_input_tokens": 5,
            "_output_tokens": 5,
            "_escalation_count": 1,
            "_tools_called": [],
            "_tools_declared": [],
        })
        mock_instance._resolve_model.return_value = "gpt-4o"
        await harness.run({})

    warning_events = [e for e in events if e[0] == "tier_escalation_warning"]
    assert len(warning_events) == 1
    assert warning_events[0][1]["stage"] == "s"
    assert warning_events[0][1]["escalation_count"] >= 1


@pytest.mark.asyncio
async def test_no_escalation_no_warning_event(tmp_path):
    """Engine does not emit tier_escalation_warning when escalation_count is 0."""
    events: list[tuple[str, dict]] = []

    def capture(name, data):
        events.append((name, data))

    spec = _spec_with_guided_json_small()
    harness = Harness(spec=spec, session_dir=tmp_path, on_event=capture, validate=False, use_cache=False)

    with patch("armature.runtime.engine.LLMNode") as MockLLMNode:
        mock_instance = MockLLMNode.return_value
        mock_instance.execute = AsyncMock(return_value={
            "x": "hello",
            "_input_tokens": 5,
            "_output_tokens": 5,
            "_escalation_count": 0,
            "_tools_called": [],
            "_tools_declared": [],
        })
        mock_instance._resolve_model.return_value = "gpt-4o"
        await harness.run({})

    warning_events = [e for e in events if e[0] == "tier_escalation_warning"]
    assert warning_events == []
