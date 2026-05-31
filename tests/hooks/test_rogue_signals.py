"""Tests for KYA-inspired rogue signal counter in the safety hook."""
import pytest
from armature.hooks.lifecycle import (
    HookRegistry,
    HookPhase,
    HookDecision,
    RogueSignalCounter,
    SafetyHookBuilder,
    ToolBlocked,
)
from armature.spec.models import ToolSafetyRule, SafetyCondition


def _block_rule(tool: str = "bash") -> ToolSafetyRule:
    return ToolSafetyRule(
        tool=tool,
        condition=SafetyCondition(field="cmd", op="contains", value="rm"),
        action="block",
        message="blocked for test",
    )


async def _fire_pre_tool(registry: HookRegistry, tool: str = "bash", cmd: str = "rm -rf /"):
    try:
        await registry.run_pre_tool(tool, {"cmd": cmd}, {})
    except ToolBlocked:
        pass


# ── RogueSignalCounter unit tests ─────────────────────────────────────────────

def test_rogue_signal_counter_starts_at_zero():
    counter = RogueSignalCounter()
    assert counter.count == 0


def test_rogue_signal_counter_increment():
    counter = RogueSignalCounter()
    counter.increment()
    counter.increment()
    assert counter.count == 2


# ── SafetyHookBuilder counter integration ─────────────────────────────────────

async def test_blocked_tool_increments_counter():
    """When a tool is blocked, the counter is incremented."""
    counter = RogueSignalCounter()
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [_block_rule()], counter=counter)

    await _fire_pre_tool(registry)

    assert counter.count == 1


async def test_multiple_blocks_accumulate_in_counter():
    """Each block increments the counter independently."""
    counter = RogueSignalCounter()
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [_block_rule()], counter=counter)

    await _fire_pre_tool(registry)
    await _fire_pre_tool(registry)
    await _fire_pre_tool(registry)

    assert counter.count == 3


async def test_allowed_tool_does_not_increment_counter():
    """Allowed tool calls do not touch the rogue signal counter."""
    counter = RogueSignalCounter()
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [_block_rule()], counter=counter)

    # Call with a different command that doesn't match the block condition
    await registry.run_pre_tool("bash", {"cmd": "echo hello"}, {})

    assert counter.count == 0


async def test_no_counter_provided_still_blocks():
    """SafetyHookBuilder works without a counter (backward compatible)."""
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [_block_rule()])

    with pytest.raises(ToolBlocked):
        await registry.run_pre_tool("bash", {"cmd": "rm -rf /"}, {})
