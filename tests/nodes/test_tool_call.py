"""Tests for ToolCallNode — direct tool dispatch without an LLM."""
import pytest
from armature.nodes.tool_call import ToolCallNode, _render_args
from armature.spec.models import Stage, ToolCallConfig
from armature.registry.registry import ToolRegistry, ToolDescriptor
from armature.permissions.permissions import PermissionLevel


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        async def handler(args, _name=name):
            return {"tool": _name, "args": args}
        reg.register(ToolDescriptor(
            name=name,
            description=f"Tool {name}",
            permission=PermissionLevel.READ_ONLY,
            handler=handler,
            parameters={"input": {"type": "string"}},
        ))
    return reg


def make_stage(tool_name: str, args: dict | None = None) -> Stage:
    return Stage(
        id="test",
        tool_call=ToolCallConfig(name=tool_name, args=args or {}),
        depends_on=[],
    )


# ── ToolCallConfig model ────────────────────────────────────────────────────

def test_tool_call_config_parses():
    cfg = ToolCallConfig(name="search", args={"query": "test"})
    assert cfg.name == "search"
    assert cfg.args == {"query": "test"}


def test_tool_call_config_empty_args():
    cfg = ToolCallConfig(name="ping")
    assert cfg.args == {}


def test_stage_parses_tool_call_section():
    stage = Stage.model_validate({
        "id": "s",
        "tool_call": {"name": "run_scan", "args": {"dir": "/tmp"}},
        "depends_on": [],
    })
    assert stage.tool_call is not None
    assert stage.tool_call.name == "run_scan"
    assert stage.tool_call.args == {"dir": "/tmp"}


# ── ToolCallNode.execute ─────────────────────────────────────────────────────

async def test_executes_tool_and_returns_result():
    stage = make_stage("search", {"input": "armature"})
    reg = make_registry("search")
    node = ToolCallNode(stage=stage, registry=reg)
    result = await node.execute({})
    assert result["tool"] == "search"
    assert result["args"]["input"] == "armature"


async def test_raises_for_unknown_tool():
    stage = make_stage("nonexistent")
    reg = make_registry("search")
    node = ToolCallNode(stage=stage, registry=reg)
    with pytest.raises(KeyError):
        await node.execute({})


async def test_raises_if_no_tool_call_config():
    stage = Stage(id="s", depends_on=[])
    with pytest.raises(ValueError, match="tool_call"):
        ToolCallNode(stage=stage, registry=make_registry())


# ── Args rendering ───────────────────────────────────────────────────────────

def test_render_args_resolves_string_template():
    args = {"dir": "{{ workspace }}", "timeout": 30}
    context = {"workspace": "/tmp/repo"}
    result = _render_args(args, context)
    assert result["dir"] == "/tmp/repo"
    assert result["timeout"] == 30


def test_render_args_passes_non_strings_through():
    args = {"count": 5, "flag": True, "names": ["a", "b"]}
    result = _render_args(args, {})
    assert result == {"count": 5, "flag": True, "names": ["a", "b"]}


def test_render_args_resolves_nested_context():
    args = {"path": "{{ fetch.workspace_dir }}/scan"}
    context = {"fetch": {"workspace_dir": "/tmp/run-42"}}
    result = _render_args(args, context)
    assert result["path"] == "/tmp/run-42/scan"


def test_render_args_undefined_variable_empty_string():
    args = {"dir": "{{ missing_var }}"}
    result = _render_args(args, {})
    assert result["dir"] == ""


def test_render_args_no_templates_returns_unchanged():
    args = {"dir": "/fixed/path", "count": 3}
    result = _render_args(args, {"anything": "ignored"})
    assert result == {"dir": "/fixed/path", "count": 3}


async def test_execute_with_template_args():
    stage = make_stage("search", {"input": "{{ topic }}"})
    reg = make_registry("search")
    node = ToolCallNode(stage=stage, registry=reg)
    result = await node.execute({"topic": "AI safety"})
    assert result["args"]["input"] == "AI safety"


# ── Engine integration ────────────────────────────────────────────────────────

async def test_engine_executes_tool_call_stage(tmp_path):
    from armature.runtime.engine import Harness
    from armature.spec.models import (
        HarnessSpec, Stage, ToolCallConfig, ModelTiers, ModelTierConfig
    )

    # Register a custom tool that the spec will call
    async def my_tool(args: dict) -> dict:
        return {"scanned": True, "dir": args.get("dir", "")}

    spec = HarnessSpec(
        name="test_tool_call_wf",
        stages=[
            Stage(
                id="scan",
                tool_call=ToolCallConfig(name="my_scanner", args={"dir": "{{ workspace }}"}),
                depends_on=[],
            )
        ],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="anthropic", model="x")),
    )

    harness = Harness(spec=spec, session_dir=tmp_path)
    # Manually register the tool after harness init
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel
    harness._registry.register(ToolDescriptor(
        name="my_scanner", description="scanner", permission=PermissionLevel.READ_ONLY,
        handler=my_tool, parameters={"dir": {"type": "string"}},
    ))

    result = await harness.run({"workspace": "/tmp/test"})
    assert result["scan"]["scanned"] is True
    assert result["scan"]["dir"] == "/tmp/test"


async def test_engine_tool_call_result_available_to_downstream(tmp_path):
    """A tool_call stage result is stored in context and visible to downstream stages."""
    from unittest.mock import patch, MagicMock
    from armature.runtime.engine import Harness
    from armature.spec.models import (
        HarnessSpec, Stage, ToolCallConfig, Role, RoleType,
        ModelTiers, ModelTierConfig
    )
    from armature.registry.registry import ToolDescriptor
    from armature.permissions.permissions import PermissionLevel

    async def counter_tool(args: dict) -> dict:
        return {"count": 42}

    spec = HarnessSpec(
        name="test_downstream_wf",
        stages=[
            Stage(id="count", tool_call=ToolCallConfig(name="counter"), depends_on=[]),
            Stage(
                id="report",
                role=Role(name="R", type=RoleType.WORKER, description="Report count: {{ count.count }}", model_tier="small"),
                depends_on=["count"],
            ),
        ],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )

    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._registry.register(ToolDescriptor(
        name="counter", description="count", permission=PermissionLevel.READ_ONLY,
        handler=counter_tool, parameters={},
    ))

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "Report: 42 items found"
    response.choices[0].message.tool_calls = None
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5

    captured_system: list[str] = []

    async def mock_completion(**kwargs):
        for m in kwargs["messages"]:
            if m["role"] == "system":
                captured_system.append(m["content"])
        return response

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await harness.run({})

    assert result["count"]["count"] == 42
    # Description was rendered with the tool result
    assert "42" in captured_system[0]
