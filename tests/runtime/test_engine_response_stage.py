"""Tests for Harness wiring of on_token to LLMNode when response_stage is True."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from armature.spec.models import Stage, HarnessSpec, ModelTiers, ModelTierConfig, Role, RoleType
from armature.runtime.engine import Harness


def _llm_response(content: str = "done"):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage = None
    return r


def _make_stage(sid: str, response_stage: bool = False) -> Stage:
    return Stage(
        id=sid,
        role=Role(name="r", type=RoleType.WORKER, description="test", model_tier="small"),
        depends_on=[],
        response_stage=response_stage,
    )


def _make_harness(stages: list[Stage], tmp_path: Path) -> Harness:
    spec = HarnessSpec(
        name="wf",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


async def test_engine_passes_on_token_for_response_stage(tmp_path):
    """Engine sets on_token on LLMNode when stage.response_stage is True and harness._on_token is set."""
    stage = _make_stage("respond", response_stage=True)
    harness = _make_harness([stage], tmp_path)

    captured_on_token = {}
    sentinel = object()
    harness._on_token = sentinel

    original_init = Harness._execute_stage_with_recovery

    # Intercept LLMNode construction
    from armature.nodes import llm as llm_mod
    original_llm_init = llm_mod.LLMNode.__init__

    def capturing_init(self_node, **kwargs):
        captured_on_token["value"] = kwargs.get("on_token")
        original_llm_init(self_node, **kwargs)

    with (
        patch("armature.nodes.llm.litellm_completion", return_value=_llm_response()),
        patch.object(llm_mod.LLMNode, "__init__", capturing_init),
    ):
        try:
            await harness.run({})
        except Exception:
            pass  # result doesn't matter; we only care about on_token

    assert captured_on_token.get("value") is sentinel


async def test_engine_does_not_pass_on_token_for_normal_stage(tmp_path):
    """Engine passes on_token=None to LLMNode when stage.response_stage is False."""
    stage = _make_stage("process", response_stage=False)
    harness = _make_harness([stage], tmp_path)

    captured_on_token = {}
    harness._on_token = object()  # set but should not propagate to normal stage

    from armature.nodes import llm as llm_mod
    original_llm_init = llm_mod.LLMNode.__init__

    def capturing_init(self_node, **kwargs):
        captured_on_token["value"] = kwargs.get("on_token")
        original_llm_init(self_node, **kwargs)

    with (
        patch("armature.nodes.llm.litellm_completion", return_value=_llm_response()),
        patch.object(llm_mod.LLMNode, "__init__", capturing_init),
    ):
        try:
            await harness.run({})
        except Exception:
            pass

    assert captured_on_token.get("value") is None
