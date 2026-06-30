from __future__ import annotations
import logging
import re
import warnings
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Any

_safety_log = logging.getLogger("armature.safety")

if TYPE_CHECKING:
    from armature.spec.models import ToolSafetyRule
    from armature.registry.registry import ToolRegistry
    from armature.state.traces import TraceRecord


class HookPhase(str, Enum):
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_STAGE = "pre_stage"
    POST_STAGE = "post_stage"


class HookDecision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    MODIFY = "modify"
    REQUIRE_APPROVAL = "require_approval"


class ToolBlocked(Exception):
    def __init__(self, tool_name: str, cmd: str, message: str) -> None:
        super().__init__(f"Tool '{tool_name}' blocked: {message} (cmd={cmd!r})")
        self.tool_name = tool_name
        self.cmd = cmd
        self.message = message


class PostconditionFailed(Exception):
    def __init__(self, tool_name: str, result: Any) -> None:
        super().__init__(f"Postcondition failed for tool '{tool_name}'")
        self.tool_name = tool_name
        self.result = result


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
    if condition is None:
        return True  # no condition => matches every call of this tool
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


@dataclass
class RogueSignalCounter:
    """Counts tool-block events during a harness run for KYA-style rogue signal tracking."""
    count: int = 0

    def increment(self) -> None:
        self.count += 1


class SafetyHookBuilder:
    @staticmethod
    def register(
        registry: HookRegistry,
        rules: "list[ToolSafetyRule]",
        tool_registry: "ToolRegistry | None" = None,
        strict_mode: bool = False,
        counter: "RogueSignalCounter | None" = None,
    ) -> None:
        if not rules and not strict_mode:
            return

        async def safety_hook(phase: HookPhase, tool_name: str, args: dict, ctx: dict) -> HookDecision:
            tool_desc = tool_registry.get(tool_name) if tool_registry else None
            enhanced_args = {
                **args,
                "_tool_reversibility": tool_desc.reversibility.value if tool_desc else "unknown",
            }

            for rule in rules:
                if rule.tool != "*" and rule.tool != tool_name:
                    continue
                if not _evaluate_condition(rule.condition, enhanced_args):
                    continue

                if rule.action == "allow":
                    return HookDecision.ALLOW
                if rule.action == "block":
                    if counter is not None:
                        counter.increment()
                    raise ToolBlocked(tool_name, args.get("cmd", ""), rule.message)
                if rule.action == "require_approval":
                    answer = input(
                        f"[armature] Tool '{tool_name}' requires approval: {rule.message}\n"
                        f"  args={args}\n"
                        "  Allow? [y/N]: "
                    ).strip().lower()
                    if answer == "y":
                        return HookDecision.ALLOW
                    if counter is not None:
                        counter.increment()
                    raise ToolBlocked(tool_name, args.get("cmd", ""), rule.message)
                if rule.action == "warn":
                    warnings.warn(
                        f"[armature safety] Tool '{tool_name}': {rule.message}",
                        stacklevel=2,
                    )
                elif rule.action == "log":
                    _safety_log.info("tool=%s rule=%s msg=%s", tool_name, rule.tool, rule.message)

            if strict_mode:
                if counter is not None:
                    counter.increment()
                return HookDecision.BLOCK
            return HookDecision.ALLOW

        registry.register(HookPhase.PRE_TOOL, safety_hook)


# ── Trace-triggered Behaviors ──────────────────────────────────────────────────


@dataclass
class BehaviorRule:
    """Reactive rule that fires a handler when a trace pattern is matched."""
    name: str
    description: str
    pattern: Callable[["list[TraceRecord]"], bool]
    handler: Callable[["list[TraceRecord]"], None]


class BehaviorRegistry:
    """Registry of BehaviorRules evaluated against recent traces after each run."""

    def __init__(self) -> None:
        self._rules: list[BehaviorRule] = []

    def register(self, rule: BehaviorRule) -> None:
        self._rules.append(rule)

    def evaluate(self, traces: "list[TraceRecord]") -> None:
        for rule in self._rules:
            if rule.pattern(traces):
                rule.handler(traces)


# ── HQS feedback built-in behavior ────────────────────────────────────────────

def _compute_simple_hqs(traces: "list[TraceRecord]") -> float:
    n = len(traces)
    if n == 0:
        return 1.0
    output_valid_rate = sum(1 for t in traces if t.output_valid) / n
    success_rate = sum(1 for t in traces if t.success) / n
    avg_latency = sum(t.latency_ms for t in traces) / n
    latency_score = max(0.0, 1.0 - avg_latency / 5000.0)
    return 0.40 * output_valid_rate + 0.30 * success_rate + 0.20 * 0.5 + 0.10 * latency_score


def _hqs_feedback_pattern(traces: "list[TraceRecord]") -> bool:
    recent = traces[-10:]
    if len(recent) < 3:
        return False
    return _compute_simple_hqs(recent) < 0.75


def _hqs_feedback_handler(traces: "list[TraceRecord]") -> None:
    import sys
    print(
        "\n[armature] HQS hint: quality below 0.75 over recent traces — "
        "consider running `armature improve <spec>`",
        file=sys.stderr,
    )


def make_default_behavior_registry() -> BehaviorRegistry:
    """Return a BehaviorRegistry pre-loaded with the hqs_feedback built-in."""
    registry = BehaviorRegistry()
    registry.register(BehaviorRule(
        name="hqs_feedback",
        description="Suggest improvement when HQS drops below 0.75",
        pattern=_hqs_feedback_pattern,
        handler=_hqs_feedback_handler,
    ))
    return registry
