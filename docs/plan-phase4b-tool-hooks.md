# Phase 4B: Tool-Level Safety Hooks (AgentSpec Pattern) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the harness declarative, YAML-authored safety rules that intercept adapter ("tool") calls before execution — the AgentSpec paper's primary contribution — so operators can enforce policy without writing Python hooks.

**Architecture:** A new `ToolSafetyRule` model (with structured `SafetyCondition`) is added to `HarnessSpec`. A `SafetyHookBuilder` converts those declarative rules into a programmatic hook registered on `HookRegistry.PRE_TOOL`. The engine already has `run_pre_tool` / `run_post_tool` plumbing; this plan wires those calls around `ScriptNode` execution so they are actually invoked. A new `ToolBlocked` exception distinguishes deliberate policy blocks from infrastructure errors.

**Tech Stack:** Python 3.11+, Pydantic v2, asyncio, `re` (stdlib), pytest-asyncio

---

## Background

The paper *AgentSpec: Specifying Agent Behavior Through Composable, Interpretable Constraints* makes a central argument: safety rules should be first-class citizens of the workflow specification, not scattered Python callbacks. The current harness has the hook infrastructure (`HookRegistry.run_pre_tool/run_post_tool`, `HookDecision.BLOCK`) but:

1. No declarative rule format — callers must write Python hook functions
2. `run_pre_tool` / `run_post_tool` are never called by the engine — the plumbing exists but is disconnected
3. There is no `ToolBlocked` exception to signal a deliberate policy block vs. an infra error

This plan closes all three gaps with minimal surface area. Conditions use a safe structured format (no `eval`, no arbitrary Python execution).

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `armature/spec/models.py` | **Modify** | Add `SafetyCondition`, `ToolSafetyRule`; add `safety_rules` to `HarnessSpec` |
| `armature/hooks/lifecycle.py` | **Modify** | Add `ToolBlocked` exception; add `SafetyHookBuilder.from_rules()` |
| `armature/runtime/engine.py` | **Modify** | Wire `run_pre_tool`/`run_post_tool` around `ScriptNode`; register safety rules from spec |
| `tests/hooks/test_safety_rules.py` | **Create** | Unit tests for `SafetyHookBuilder` condition evaluation and hook dispatch |
| `tests/spec/test_models.py` | **Modify** | Tests for new `ToolSafetyRule` model and `HarnessSpec.safety_rules` field |
| `tests/runtime/test_engine.py` | **Modify** | Tests for tool-level hook wiring in engine (block + allow paths) |

---

## Task 1: Declarative Safety Rule Models

**Files:**
- Modify: `armature/spec/models.py`
- Modify: `tests/spec/test_models.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/spec/test_models.py`:

```python
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType,
    SafetyCondition, ToolSafetyRule,
)


def test_safety_condition_defaults():
    cond = SafetyCondition(field="cmd", op="contains", value="rm -rf")
    assert cond.field == "cmd"
    assert cond.op == "contains"
    assert cond.value == "rm -rf"


def test_tool_safety_rule_defaults():
    rule = ToolSafetyRule(
        tool="run_shell",
        condition=SafetyCondition(field="cmd", op="contains", value="rm -rf"),
        action="block",
    )
    assert rule.action == "block"
    assert rule.message == ""
    assert rule.tool == "run_shell"


def test_tool_safety_rule_wildcard():
    rule = ToolSafetyRule(
        tool="*",
        condition=SafetyCondition(field="cmd", op="truthy", value=""),
        action="log",
        message="auditing all tool calls",
    )
    assert rule.tool == "*"


def test_harness_spec_safety_rules_default_empty():
    spec = HarnessSpec(
        name="safe-flow",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="d"))],
    )
    assert spec.safety_rules == []


def test_harness_spec_accepts_safety_rules():
    spec = HarnessSpec(
        name="guarded-flow",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="d"))],
        safety_rules=[
            ToolSafetyRule(
                tool="*",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="no sudo",
            )
        ],
    )
    assert len(spec.safety_rules) == 1
    assert spec.safety_rules[0].action == "block"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bryansparks/projects/armature
python -m pytest tests/spec/test_models.py -k "safety" -v
```

Expected: `ImportError: cannot import name 'SafetyCondition' from 'armature.spec.models'`

- [ ] **Step 3: Add the models to `armature/spec/models.py`**

Add these classes after the `Failure` model (before `FileState`):

```python
class SafetyCondition(BaseModel):
    field: str
    op: Literal["contains", "not_contains", "equals", "not_equals", "matches_regex", "truthy"]
    value: str = ""


class ToolSafetyRule(BaseModel):
    tool: str                                            # adapter name or "*" for all
    condition: SafetyCondition
    action: Literal["block", "warn", "log"]
    message: str = ""
```

Also add `from typing import Literal` to the imports at the top of `armature/spec/models.py` (it currently imports `Any` from typing — extend the import):

```python
from typing import Any, Literal
```

Then add `safety_rules` to `HarnessSpec`:

```python
class HarnessSpec(BaseModel):
    name: str
    version: str = "1.0"
    description: str = ""
    contracts: Contract = Field(default_factory=Contract)
    roles: dict[str, Role] = Field(default_factory=dict)
    stages: list[Stage]
    adapters: dict[str, Adapter] = Field(default_factory=dict)
    failures: dict[str, Failure] = Field(default_factory=dict)
    model_tiers: ModelTiers = Field(default_factory=ModelTiers)
    file_state: FileState = Field(default_factory=FileState)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    safety_rules: list[ToolSafetyRule] = Field(default_factory=list)   # NEW
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/spec/test_models.py -k "safety" -v
```

Expected: 5 tests PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

```bash
python -m pytest tests/spec/ -v
```

Expected: All existing spec tests still pass.

- [ ] **Step 6: Commit**

```bash
git add armature/spec/models.py tests/spec/test_models.py
git commit -m "feat: add ToolSafetyRule declarative model to HarnessSpec"
```

---

## Task 2: SafetyHookBuilder — Condition Evaluation and Hook Factory

**Files:**
- Modify: `armature/hooks/lifecycle.py`
- Create: `tests/hooks/test_safety_rules.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/hooks/test_safety_rules.py`:

```python
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
    decision = await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})
    assert decision == HookDecision.BLOCK


async def test_contains_no_match_allows():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="contains", value="rm -rf", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "ls -la"}, {})
    assert decision == HookDecision.ALLOW


async def test_not_contains_blocks_when_value_absent():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="not_contains", value="safe", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "rm -rf /tmp"}, {})
    assert decision == HookDecision.BLOCK


async def test_equals_blocks_exact_match():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="equals", value="shutdown", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "shutdown"}, {})
    assert decision == HookDecision.BLOCK


async def test_not_equals_blocks_non_match():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="not_equals", value="allowed-cmd", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "other-cmd"}, {})
    assert decision == HookDecision.BLOCK


async def test_matches_regex_blocks():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="matches_regex", value=r"sudo\s+", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "sudo apt-get install"}, {})
    assert decision == HookDecision.BLOCK


async def test_truthy_blocks_when_field_present_and_nonempty():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(op="truthy", value="", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "echo hi"}, {})
    assert decision == HookDecision.BLOCK


async def test_missing_field_does_not_match():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(field="nonexistent", op="contains", value="x", action="block")])
    decision = await registry.run_pre_tool("shell", {"cmd": "ls"}, {})
    assert decision == HookDecision.ALLOW


# --- Tool name matching ---

async def test_wildcard_matches_any_tool():
    registry = HookRegistry()
    SafetyHookBuilder.register(registry, [make_rule(tool="*", action="block")])
    decision = await registry.run_pre_tool("anything", {"cmd": "rm -rf /"}, {})
    assert decision == HookDecision.BLOCK


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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/hooks/test_safety_rules.py -v
```

Expected: `ImportError: cannot import name 'SafetyHookBuilder' from 'armature.hooks.lifecycle'`

- [ ] **Step 3: Add `ToolBlocked` and `SafetyHookBuilder` to `armature/hooks/lifecycle.py`**

Replace the entire file with:

```python
from __future__ import annotations
import re
import warnings
from enum import Enum
from typing import TYPE_CHECKING, Callable, Any

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
                    return HookDecision.BLOCK
                if rule.action == "warn":
                    warnings.warn(
                        f"[armature safety] Tool '{tool_name}': {rule.message}",
                        stacklevel=2,
                    )
                    # fall through to ALLOW
            return HookDecision.ALLOW

        registry.register(HookPhase.PRE_TOOL, safety_hook)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/hooks/test_safety_rules.py -v
```

Expected: 14 tests PASS

- [ ] **Step 5: Run full hooks test suite to verify no regressions**

```bash
python -m pytest tests/hooks/ -v
```

Expected: All 3 original lifecycle tests + 14 new safety tests pass.

- [ ] **Step 6: Commit**

```bash
git add armature/hooks/lifecycle.py tests/hooks/test_safety_rules.py
git commit -m "feat: add SafetyHookBuilder with declarative condition evaluation"
```

---

## Task 3: Wire Pre/Post-Tool Hooks in Engine + Register from Spec

**Files:**
- Modify: `armature/runtime/engine.py`
- Modify: `tests/runtime/test_engine.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/runtime/test_engine.py`:

```python
from unittest.mock import AsyncMock, patch
from armature.hooks.lifecycle import HookDecision, ToolBlocked
from armature.spec.models import (
    HarnessSpec, Stage, Adapter, SafetyCondition, ToolSafetyRule,
)


def make_adapter_spec(adapter_name: str = "run_shell", cmd: str = "echo hi") -> HarnessSpec:
    return HarnessSpec(
        name="adapter-test",
        stages=[Stage(id="s1", adapter=adapter_name)],
        adapters={adapter_name: Adapter(name=adapter_name, type="script", cmd=cmd)},
    )


async def test_pre_tool_hook_is_called_for_adapter(tmp_path):
    spec = make_adapter_spec(cmd="echo hello")
    harness = Harness(spec=spec, session_dir=tmp_path)

    calls = []

    async def capture_hook(phase, tool_name, args, ctx):
        calls.append(tool_name)
        return HookDecision.ALLOW

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.PRE_TOOL, capture_hook)

    await harness.run({})
    assert calls == ["run_shell"]


async def test_pre_tool_hook_block_raises_tool_blocked(tmp_path):
    spec = make_adapter_spec(cmd="rm -rf /tmp/test")
    harness = Harness(spec=spec, session_dir=tmp_path)

    async def block_hook(phase, tool_name, args, ctx):
        return HookDecision.BLOCK

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.PRE_TOOL, block_hook)

    with pytest.raises(ToolBlocked) as exc_info:
        await harness.run({})
    assert "run_shell" in str(exc_info.value)


async def test_post_tool_hook_is_called_after_adapter(tmp_path):
    spec = make_adapter_spec(cmd="echo done")
    harness = Harness(spec=spec, session_dir=tmp_path)

    post_calls = []

    async def post_hook(phase, tool_name, result, ctx):
        post_calls.append((tool_name, result.get("exit_code")))

    from armature.hooks.lifecycle import HookPhase
    harness._hooks.register(HookPhase.POST_TOOL, post_hook)

    await harness.run({})
    assert post_calls == [("run_shell", 0)]


async def test_safety_rules_from_spec_block_adapter(tmp_path):
    spec = HarnessSpec(
        name="guarded",
        stages=[Stage(id="s1", adapter="danger")],
        adapters={"danger": Adapter(name="danger", type="script", cmd="sudo apt-get install vim")},
        safety_rules=[
            ToolSafetyRule(
                tool="danger",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="sudo not permitted",
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    with pytest.raises(ToolBlocked) as exc_info:
        await harness.run({})
    assert "sudo not permitted" in str(exc_info.value)


async def test_safety_rules_allow_safe_adapter(tmp_path):
    spec = HarnessSpec(
        name="guarded-allow",
        stages=[Stage(id="s1", adapter="safe_cmd")],
        adapters={"safe_cmd": Adapter(name="safe_cmd", type="script", cmd="echo hello")},
        safety_rules=[
            ToolSafetyRule(
                tool="safe_cmd",
                condition=SafetyCondition(field="cmd", op="contains", value="sudo"),
                action="block",
                message="sudo not permitted",
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    result = await harness.run({})
    assert result["s1"]["exit_code"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/runtime/test_engine.py -k "tool_hook or safety_rule" -v
```

Expected: Tests fail — hooks not wired in engine.

- [ ] **Step 3: Wire hooks in `armature/runtime/engine.py`**

**3a. Register safety rules from spec in `__init__`** — add after `self._hooks = HookRegistry()`:

```python
        self._hooks = HookRegistry()
        if self._spec.safety_rules:
            from armature.hooks.lifecycle import SafetyHookBuilder
            SafetyHookBuilder.register(self._hooks, self._spec.safety_rules)
```

**3b. Wire `run_pre_tool` / `run_post_tool` around ScriptNode in `_execute_stage`** — replace the `elif stage.adapter:` block:

```python
                    elif stage.adapter:
                        adapter = self._spec.adapters.get(stage.adapter)
                        if adapter is None:
                            raise ValueError(f"Adapter '{stage.adapter}' not defined in spec")
                        node = ScriptNode(adapter=adapter)
                        tool_args = {"cmd": adapter.cmd or ""}
                        decision = await self._hooks.run_pre_tool(stage.adapter, tool_args, context)
                        if decision == HookDecision.BLOCK:
                            from armature.hooks.lifecycle import ToolBlocked
                            raise ToolBlocked(stage.adapter, adapter.cmd or "", "blocked by safety rule")
                        result = await node.execute(context)
                        await self._hooks.run_post_tool(stage.adapter, result, context)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/runtime/test_engine.py -k "tool_hook or safety_rule" -v
```

Expected: 5 new tests PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

```bash
python -m pytest tests/ -v --ignore=tests/integration
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add armature/runtime/engine.py tests/runtime/test_engine.py
git commit -m "feat: wire pre/post-tool hooks in engine; register safety_rules from HarnessSpec"
```

---

## Self-Review

### Spec Coverage

| Requirement | Task |
|-------------|------|
| Declarative `ToolSafetyRule` in YAML spec | Task 1 |
| Structured conditions (no eval): contains, equals, regex, truthy | Task 2 |
| `block` / `warn` / `log` actions | Task 2 |
| Wildcard `tool: "*"` matches all adapters | Task 2 |
| Pre-tool hook fires before ScriptNode.execute() | Task 3 |
| Post-tool hook fires after ScriptNode.execute() | Task 3 |
| Safety rules auto-registered from spec at Harness init | Task 3 |
| `ToolBlocked` exception distinguishes policy block from infra error | Task 2+3 |

### Placeholder Scan

None. All steps contain complete, runnable code.

### Type Consistency

- `SafetyCondition.op` declared as `Literal[...]` in Task 1; consumed by `_evaluate_condition` in Task 2 via `condition.op` — consistent.
- `ToolSafetyRule.tool` is `str` throughout — consistent.
- `ToolBlocked(tool_name, cmd, message)` constructor in Task 2; raised as `ToolBlocked(stage.adapter, adapter.cmd or "", "...")` in Task 3 — consistent.
- `SafetyHookBuilder.register(registry, rules)` defined in Task 2; called in Task 3 — consistent.
