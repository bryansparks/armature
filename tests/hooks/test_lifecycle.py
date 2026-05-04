import pytest
from armature.hooks.lifecycle import HookRegistry, HookDecision, HookPhase

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
