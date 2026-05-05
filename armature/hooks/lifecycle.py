from __future__ import annotations
import logging
import re
import warnings
from enum import Enum
from typing import TYPE_CHECKING, Callable, Any

_safety_log = logging.getLogger("armature.safety")

if TYPE_CHECKING:
    from armature.spec.models import ToolSafetyRule


class HookPhase(str, Enum):
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_STAGE = "pre_stage"
    POST_STAGE = "post_stage"


class HookDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    MODIFY = "modify"


class ToolBlocked(Exception):
    def __init__(self, tool_name: str, cmd: str, message: str) -> None:
        super().__init__(f"Tool '{tool_name}' blocked: {message} (cmd={cmd!r})")
        self.tool_name = tool_name
        self.cmd = cmd
        self.message = message


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


def _evaluate_condition(condition, args: dict) -> bool:
    raw = args.get(condition.field)
    value = str(raw) if raw is not None else None

    if value is None:
        return False

    op = condition.op
    if op == "contains":
        return condition.value in value
    if op == "not_contains":
        return condition.value not in value
    if op == "equals":
        return value == condition.value
    if op == "not_equals":
        return value != condition.value
    if op == "matches_regex":
        return bool(re.search(condition.value, value))
    if op == "truthy":
        return bool(value)
    return False


class SafetyHookBuilder:
    @staticmethod
    def register(registry: HookRegistry, rules: "list[ToolSafetyRule]") -> None:
        if not rules:
            return

        async def safety_hook(phase: HookPhase, tool_name: str, args: dict, ctx: dict) -> HookDecision:
            for rule in rules:
                if rule.tool != "*" and rule.tool != tool_name:
                    continue
                if not _evaluate_condition(rule.condition, args):
                    continue

                if rule.action == "block":
                    raise ToolBlocked(tool_name, args.get("cmd", ""), rule.message)
                if rule.action == "warn":
                    warnings.warn(
                        f"[armature safety] Tool '{tool_name}': {rule.message}",
                        stacklevel=2,
                    )
                elif rule.action == "log":
                    _safety_log.info("tool=%s rule=%s msg=%s", tool_name, rule.tool, rule.message)
                # all non-block actions fall through to ALLOW
            return HookDecision.ALLOW

        registry.register(HookPhase.PRE_TOOL, safety_hook)
