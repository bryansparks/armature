import pytest
from armature.hooks.lifecycle import (
    HookRegistry, HookDecision, HookPhase,
    SafetyHookBuilder, ToolBlocked,
)
from armature.spec.models import SafetyCondition, ToolSafetyRule


def make_rule(
    tool: str = "*",
    field: str = "cmd",
    op: str = "contains",
    value: str = "rm -rf",
    action: str = "block",
    message: str = "blocked",
) -> ToolSafetyRule:
    return ToolSafetyRule(
        tool=tool,
        condition=SafetyCondition(field=field, op=op, value=value),
        action=action,
        message=message,
    )


# --- Condition evaluation ---

async def test_contains_matches():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="contains", value="rm -rf", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})


async def test_contains_no_match_allows():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="contains", value="rm -rf", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "ls -la"}, {})
    assert decision == HookDecision.ALLOW


async def test_not_contains_blocks_when_value_absent():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="not_contains", value="safe", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})


async def test_equals_blocks_exact_match():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="equals", value="shutdown", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("shell", {"cmd": "shutdown"}, {})


async def test_not_equals_blocks_non_match():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="not_equals", value="allowed-cmd", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("shell", {"cmd": "other-cmd"}, {})


async def test_matches_regex_blocks():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="matches_regex", value=r"sudo\s+", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("shell", {"cmd": "sudo apt-get install"}, {})


async def test_truthy_blocks_when_field_present_and_nonempty():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="truthy", value="", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("shell", {"cmd": "echo hi"}, {})


async def test_missing_field_does_not_match():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(field="nonexistent", op="contains", value="x", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "ls"}, {})
    assert decision == HookDecision.ALLOW


# --- Tool name matching ---

async def test_wildcard_matches_any_tool():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(tool="*", action="block")])
    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("anything", {"cmd": "rm -rf /"}, {})


async def test_specific_tool_does_not_match_other_tool():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(tool="dangerous_cmd", action="block")])
    decision = await registry.run_pre_tool("safe_cmd", {"cmd": "rm -rf /"}, {})
    assert decision == HookDecision.ALLOW


# --- Actions ---

async def test_warn_action_allows():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(action="warn")])
    decision = await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})
    assert decision == HookDecision.ALLOW


async def test_log_action_allows():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(action="log")])
    decision = await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})
    assert decision == HookDecision.ALLOW


# --- ToolBlocked exception ---

def test_tool_blocked_is_exception():
    exc = ToolBlocked("shell", "rm -rf /tmp", "destructive command")
    assert isinstance(exc, Exception)
    assert "shell" in str(exc)
