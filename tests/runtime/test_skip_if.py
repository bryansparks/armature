"""Tests for skip_if conditional stage execution."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from armature.spec.models import (
    Stage, Role, RoleType, HarnessSpec, ModelTiers, ModelTierConfig,
    ToolCallConfig,
)
from armature.runtime.engine import Harness


# ── Model field ───────────────────────────────────────────────────────────────

def test_stage_accepts_skip_if():
    stage = Stage(id="s", skip_if="{{ quality >= 0.9 }}", depends_on=[])
    assert stage.skip_if == "{{ quality >= 0.9 }}"


def test_stage_skip_if_defaults_none():
    stage = Stage(id="s", depends_on=[])
    assert stage.skip_if is None


def test_stage_parses_skip_if_from_yaml():
    stage = Stage.model_validate({
        "id": "s",
        "skip_if": "{{ score > 0.8 }}",
        "depends_on": [],
    })
    assert stage.skip_if == "{{ score > 0.8 }}"


# ── Engine: skip logic ────────────────────────────────────────────────────────

def _make_harness(stages: list[Stage], tmp_path: Path) -> Harness:
    spec = HarnessSpec(
        name="skip_test",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    return Harness(spec=spec, session_dir=tmp_path)


async def test_stage_skipped_when_skip_if_true(tmp_path):
    """Stage returns {"_skipped": True} when skip_if renders truthy."""
    async def my_tool(args): return {"ran": True}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [
        Stage(id="always_skip", skip_if="{{ True }}", tool_call=ToolCallConfig(name="t"), depends_on=[]),
    ]
    harness = _make_harness(stages, tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=my_tool, parameters={},
    ))

    result = await harness.run({})
    assert result["always_skip"] == {"_skipped": True}


async def test_stage_runs_when_skip_if_false(tmp_path):
    """Stage executes normally when skip_if renders falsy."""
    async def my_tool(args): return {"ran": True}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [
        Stage(id="s", skip_if="{{ False }}", tool_call=ToolCallConfig(name="t"), depends_on=[]),
    ]
    harness = _make_harness(stages, tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=my_tool, parameters={},
    ))

    result = await harness.run({})
    assert result["s"]["ran"] is True


async def test_skip_if_evaluates_context(tmp_path):
    """skip_if is Jinja2-rendered against context — upstream results are accessible."""
    async def scorer(args): return {"score": 0.95}
    async def reporter(args): return {"report": "done"}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [
        Stage(id="score", tool_call=ToolCallConfig(name="scorer"), depends_on=[]),
        Stage(id="report", skip_if="{{ score.score >= 0.9 }}", tool_call=ToolCallConfig(name="reporter"), depends_on=["score"]),
    ]
    harness = _make_harness(stages, tmp_path)
    harness._registry.register(ToolDescriptor(
        name="scorer", description="s", permission=PermissionLevel.READ_ONLY,
        handler=scorer, parameters={},
    ))
    harness._registry.register(ToolDescriptor(
        name="reporter", description="r", permission=PermissionLevel.READ_ONLY,
        handler=reporter, parameters={},
    ))

    result = await harness.run({})
    assert result["score"]["score"] == 0.95
    assert result["report"] == {"_skipped": True}


async def test_skip_if_false_upstream_result_runs_stage(tmp_path):
    """Stage runs when upstream result makes skip_if evaluate to False."""
    async def scorer(args): return {"score": 0.5}
    async def reporter(args): return {"report": "done"}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [
        Stage(id="score", tool_call=ToolCallConfig(name="scorer"), depends_on=[]),
        Stage(id="report", skip_if="{{ score.score >= 0.9 }}", tool_call=ToolCallConfig(name="reporter"), depends_on=["score"]),
    ]
    harness = _make_harness(stages, tmp_path)
    harness._registry.register(ToolDescriptor(
        name="scorer", description="s", permission=PermissionLevel.READ_ONLY,
        handler=scorer, parameters={},
    ))
    harness._registry.register(ToolDescriptor(
        name="reporter", description="r", permission=PermissionLevel.READ_ONLY,
        handler=reporter, parameters={},
    ))

    result = await harness.run({})
    assert result["report"]["report"] == "done"


async def test_skip_if_missing_var_does_not_skip(tmp_path):
    """Undefined variables render to empty string — ChainableUndefined — so skip_if is falsy."""
    async def my_tool(args): return {"ran": True}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [
        Stage(id="s", skip_if="{{ nonexistent_var }}", tool_call=ToolCallConfig(name="t"), depends_on=[]),
    ]
    harness = _make_harness(stages, tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=my_tool, parameters={},
    ))

    result = await harness.run({})
    # Missing var → empty string → not in ("true","1","yes") → stage runs
    assert result["s"]["ran"] is True


async def test_skip_if_with_on_event_emits_skipped(tmp_path):
    """Skipped stages emit stage_skipped via on_event callback."""
    async def my_tool(args): return {}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [
        Stage(id="s", skip_if="{{ True }}", tool_call=ToolCallConfig(name="t"), depends_on=[]),
    ]
    events: list[tuple[str, dict]] = []
    spec = HarnessSpec(
        name="skip_event_test",
        stages=stages,
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path, on_event=lambda e, d: events.append((e, d)))
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=my_tool, parameters={},
    ))

    await harness.run({})
    assert any(e == "stage_skipped" and d["stage"] == "s" for e, d in events)


async def test_skip_if_no_skip_if_runs_normally(tmp_path):
    """Stage without skip_if always executes — regression guard."""
    async def my_tool(args): return {"value": 42}
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    stages = [Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])]
    harness = _make_harness(stages, tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY,
        handler=my_tool, parameters={},
    ))

    result = await harness.run({})
    assert result["s"]["value"] == 42


async def test_skip_if_with_llm_stage_skipped(tmp_path):
    """LLM stages are skipped the same way as tool_call stages."""
    stages = [
        Stage(
            id="analysis",
            skip_if="{{ True }}",
            role=Role(name="A", type=RoleType.WORKER, description="analyze", model_tier="small"),
            depends_on=[],
        ),
    ]
    harness = _make_harness(stages, tmp_path)
    result = await harness.run({})
    assert result["analysis"] == {"_skipped": True}
