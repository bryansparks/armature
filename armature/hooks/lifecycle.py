from __future__ import annotations
from enum import Enum
from typing import Callable, Any


class HookPhase(str, Enum):
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_STAGE = "pre_stage"
    POST_STAGE = "post_stage"


class HookDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    MODIFY = "modify"


class HookRegistry:
    def __init__(self):
        self._hooks: dict[HookPhase, list[Callable]] = {p: [] for p in HookPhase}

    def register(self, phase: HookPhase, fn: Callable) -> None:
        self._hooks[phase].append(fn)

    async def run_pre_tool(self, tool_name: str, args: dict, ctx: dict) -> HookDecision:
        for hook in self._hooks[HookPhase.PRE_TOOL]:
            decision = await hook(HookPhase.PRE_TOOL, tool_name, args, ctx)
            if decision == HookDecision.BLOCK:
                return HookDecision.BLOCK
        return HookDecision.ALLOW

    async def run_post_tool(self, tool_name: str, result: Any, ctx: dict) -> None:
        for hook in self._hooks[HookPhase.POST_TOOL]:
            await hook(HookPhase.POST_TOOL, tool_name, result, ctx)

    async def run_pre_stage(self, stage_id: str, ctx: dict) -> HookDecision:
        for hook in self._hooks[HookPhase.PRE_STAGE]:
            decision = await hook(HookPhase.PRE_STAGE, stage_id, {}, ctx)
            if decision == HookDecision.BLOCK:
                return HookDecision.BLOCK
        return HookDecision.ALLOW

    async def run_post_stage(self, stage_id: str, result: Any, ctx: dict) -> None:
        for hook in self._hooks[HookPhase.POST_STAGE]:
            await hook(HookPhase.POST_STAGE, stage_id, result, ctx)
