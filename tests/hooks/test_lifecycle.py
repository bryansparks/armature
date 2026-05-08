import pytest
import warnings
from armature.hooks.lifecycle import (
    HookRegistry, HookDecision, HookPhase, ToolBlocked,
    SafetyHookBuilder, _evaluate_condition,
)


async def test_pre_tool_hook_allow():
    registry = HookRegistry()
    async def allow_hook(phase, tool_name, args, ctx):
        return HookDecision.ALLOW

    registry.register(HookPhase.PRE_TOOL, allow_hook)
    decision = await registry.run_pre_tool("shell", {"cmd": "ls"}, {})
    assert decision == HookDecision.ALLOW


async def test_pre_tool_hook_block():
    registry = HookRegistry()
    async def block_hook(phase, tool_name, args, ctx):
        return HookDecision.BLOCK

    registry.register(HookPhase.PRE_TOOL, block_hook)
    decision = await registry.run_pre_tool("shell", {"cmd": "rm -rf /"}, {})
    assert decision == HookDecision.BLOCK


async def test_post_tool_hook_called():
    registry = HookRegistry()
    called_with = []

    async def record_hook(phase, tool_name, result, ctx):
        called_with.append((tool_name, result))

    registry.register(HookPhase.POST_TOOL, record_hook)
    await registry.run_post_tool("shell", {"exit_code": 0}, {})
    assert called_with == [("shell", {"exit_code": 0})]


async def test_multiple_pre_tool_hooks_first_block_wins():
    """First blocking hook short-circuits; subsequent hooks not consulted."""
    registry = HookRegistry()
    second_called = []

    async def block_hook(phase, tool_name, args, ctx):
        return HookDecision.BLOCK

    async def second_hook(phase, tool_name, args, ctx):
        second_called.append(True)
        return HookDecision.ALLOW

    registry.register(HookPhase.PRE_TOOL, block_hook)
    registry.register(HookPhase.PRE_TOOL, second_hook)
    decision = await registry.run_pre_tool("shell", {}, {})
    assert decision == HookDecision.BLOCK
    assert second_called == []


async def test_no_pre_tool_hooks_returns_allow():
    registry = HookRegistry()
    decision = await registry.run_pre_tool("anything", {}, {})
    assert decision == HookDecision.ALLOW


async def test_pre_stage_hook_block():
    registry = HookRegistry()
    async def block_all(phase, stage_id, args, ctx):
        return HookDecision.BLOCK

    registry.register(HookPhase.PRE_STAGE, block_all)
    decision = await registry.run_pre_stage("my_stage", {})
    assert decision == HookDecision.BLOCK


async def test_post_stage_hook_called():
    registry = HookRegistry()
    calls = []

    async def record(phase, stage_id, result, ctx):
        calls.append(stage_id)

    registry.register(HookPhase.POST_STAGE, record)
    await registry.run_post_stage("s1", {"output": "ok"}, {})
    assert calls == ["s1"]


async def test_multiple_post_tool_hooks_all_called():
    registry = HookRegistry()
    calls = []

    async def hook_a(phase, tool_name, result, ctx):
        calls.append("a")

    async def hook_b(phase, tool_name, result, ctx):
        calls.append("b")

    registry.register(HookPhase.POST_TOOL, hook_a)
    registry.register(HookPhase.POST_TOOL, hook_b)
    await registry.run_post_tool("tool", {}, {})
    assert calls == ["a", "b"]


# ── _evaluate_condition ───────────────────────────────────────────────────────

def test_evaluate_contains_true():
    from armature.spec.models import SafetyCondition
    cond = SafetyCondition(field="cmd", op="contains", value="sudo")
    assert _evaluate_condition(cond, {"cmd": "sudo apt-get update"}) is True


def test_evaluate_contains_false():
    from armature.spec.models import SafetyCondition
    cond = SafetyCondition(field="cmd", op="contains", value="sudo")
    assert _evaluate_condition(cond, {"cmd": "echo hello"}) is False


def test_evaluate_missing_field_returns_false():
    from armature.spec.models import SafetyCondition
    cond = SafetyCondition(field="cmd", op="contains", value="x")
    assert _evaluate_condition(cond, {}) is False


def test_evaluate_matches_regex():
    from armature.spec.models import SafetyCondition
    cond = SafetyCondition(field="cmd", op="matches_regex", value=r"rm\s+-rf")
    assert _evaluate_condition(cond, {"cmd": "rm -rf /tmp"}) is True


def test_evaluate_equals():
    from armature.spec.models import SafetyCondition
    cond = SafetyCondition(field="tool", op="equals", value="shell")
    assert _evaluate_condition(cond, {"tool": "shell"}) is True
    assert _evaluate_condition(cond, {"tool": "python"}) is False


# ── SafetyHookBuilder ─────────────────────────────────────────────────────────

async def test_safety_hook_builder_block_raises_tool_blocked():
    from armature.spec.models import SafetyCondition, ToolSafetyRule
    registry = HookRegistry()
    rules = [
        ToolSafetyRule(
            tool="shell",
            condition=SafetyCondition(field="cmd", op="contains", value="rm -rf"),
            action="block",
            message="destructive command blocked",
        )
    ]
    SafetyHookBuilder.register(registry, rules)
    with pytest.raises(ToolBlocked, match="destructive command blocked"):
        await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})


async def test_safety_hook_builder_warn_action():
    from armature.spec.models import SafetyCondition, ToolSafetyRule
    registry = HookRegistry()
    rules = [
        ToolSafetyRule(
            tool="shell",
            condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
            action="warn",
            message="sudo usage detected",
        )
    ]
    SafetyHookBuilder.register(registry, rules)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decision = await registry.run_pre_tool("shell", {"cmd": "sudo apt-get install x"}, {})
    assert decision == HookDecision.ALLOW
    assert any("sudo usage detected" in str(w.message) for w in caught)


async def test_safety_hook_wildcard_tool_matches_any():
    from armature.spec.models import SafetyCondition, ToolSafetyRule
    registry = HookRegistry()
    rules = [
        ToolSafetyRule(
            tool="*",
            condition=SafetyCondition(field="cmd", op="contains", value="forbidden"),
            action="block",
            message="forbidden keyword",
        )
    ]
    SafetyHookBuilder.register(registry, rules)
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("any_tool", {"cmd": "forbidden command"}, {})


async def test_safety_hook_no_match_allows():
    from armature.spec.models import SafetyCondition, ToolSafetyRule
    registry = HookRegistry()
    rules = [
        ToolSafetyRule(
            tool="shell",
            condition=SafetyCondition(field="cmd", op="contains", value="danger"),
            action="block",
            message="blocked",
        )
    ]
    SafetyHookBuilder.register(registry, rules)
    decision = await registry.run_pre_tool("shell", {"cmd": "echo hello"}, {})
    assert decision == HookDecision.ALLOW
