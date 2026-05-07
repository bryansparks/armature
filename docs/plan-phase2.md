# Armature Phase 2 — Optimizer + Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add trace collection, guided JSON decoding with tier escalation, subagent fan-out, a FastAPI HTTP service, the Alembic trace submission hook, and the Meta-Harness self-improvement optimizer to make Armature production-ready and self-optimizing.

**Architecture:** Phase 2 layers onto the Phase 1 runtime without breaking changes. Every LLM stage now records a `TraceRecord` to a SQLite `TraceStore` (aiosqlite, already a dep). `LLMNode` gains JSON schema enforcement via litellm's `response_format` parameter and automatic tier escalation on parse failure. A new `SubagentNode` enables workflow fan-out to child specs. A FastAPI service wrapper exposes harness execution over HTTP. Tasks 7–8 implement the self-improvement flywheel: the Alembic hook submits high-quality traces for SLM fine-tuning, and the Meta-Harness optimizer is itself an Armature workflow that reads traces and proposes spec diffs — the first real dogfood test.

**Natural split point:** Tasks 1–6 are standalone infrastructure and runtime enhancements. Tasks 7–8 (self-improvement loop) depend on having real trace data and can be deferred to Phase 2B.

**Tech Stack:** Python 3.11+, aiosqlite (existing), FastAPI>=0.111, uvicorn[standard]>=0.30, httpx (existing), litellm response_format, Pydantic v2

---

## File Map

```
armature/
├── spec/
│   └── models.py               MODIFY — add output_schema, subagent_spec to Stage
├── state/
│   └── traces.py               CREATE — TraceRecord model + TraceStore (SQLite)
├── nodes/
│   ├── llm.py                  MODIFY — guided JSON (response_format), tier escalation, token capture
│   └── subagent.py             CREATE — SubagentNode (fan-out to child spec)
├── runtime/
│   └── engine.py               MODIFY — wire TraceStore, SubagentNode dispatch, trace recording
├── service/
│   ├── __init__.py             CREATE
│   ├── models.py               CREATE — RunRequest, RunResponse Pydantic models
│   └── app.py                  CREATE — FastAPI app (POST /run, GET /health, GET /runs/{run_id})
├── skills/
│   └── alembic.py              CREATE — submit_trace() + register_alembic_hook()
├── optimizer/
│   ├── __init__.py             CREATE
│   ├── workflow.yaml           CREATE — Meta-Harness optimizer spec (3-stage Armature workflow)
│   └── runner.py               CREATE — optimize(spec_path, trace_db) — loads traces, runs optimizer
└── cli.py                      MODIFY — add 'serve' and 'optimize' commands

tests/
├── state/
│   └── test_traces.py          CREATE
├── nodes/
│   └── test_subagent.py        CREATE
├── service/
│   ├── __init__.py             CREATE
│   └── test_app.py             CREATE
├── skills/
│   ├── __init__.py             CREATE
│   └── test_alembic.py         CREATE
├── optimizer/
│   ├── __init__.py             CREATE
│   └── test_optimizer.py       CREATE
└── integration/
    └── test_phase2.py          CREATE
```

---

## Task 1: TraceStore (SQLite-Backed Trace Collection)

**Files:**
- Create: `armature/state/traces.py`
- Create: `tests/state/test_traces.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/state/test_traces.py
import pytest
from pathlib import Path
from armature.state.traces import TraceStore, TraceRecord

@pytest.fixture
async def store(tmp_path):
    s = TraceStore(tmp_path / "traces.db")
    await s.init()
    return s

async def test_record_and_query(store):
    trace = TraceRecord(
        run_id="run1",
        workflow_name="my-flow",
        stage_id="s1",
        role_type="worker",
        model="ollama/qwen2.5:7b",
        input_tokens=50,
        output_tokens=20,
        latency_ms=210.5,
        success=True,
        output_valid=True,
    )
    await store.record(trace)
    results = await store.query(workflow_name="my-flow")
    assert len(results) == 1
    assert results[0].run_id == "run1"
    assert results[0].latency_ms == pytest.approx(210.5)

async def test_high_quality_filter(store):
    await store.record(TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s", role_type="judge",
        model="claude-opus-4-7", input_tokens=100, output_tokens=50,
        latency_ms=500, success=True, output_valid=True, quorum_score=0.92,
    ))
    await store.record(TraceRecord(
        run_id="r2", workflow_name="w", stage_id="s", role_type="judge",
        model="claude-opus-4-7", input_tokens=100, output_tokens=50,
        latency_ms=500, success=True, output_valid=True, quorum_score=0.55,
    ))
    hq = await store.high_quality_traces("w", min_score=0.85)
    assert len(hq) == 1
    assert hq[0].run_id == "r1"

async def test_empty_db_returns_empty(store):
    results = await store.query()
    assert results == []

async def test_init_is_idempotent(tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    await store.init()
    await store.init()  # second call must not raise
    results = await store.query()
    assert results == []
```

- [ ] **Step 2: Run tests to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/state/test_traces.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write `armature/state/traces.py`**

```python
from __future__ import annotations
import json
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field


class TraceRecord(BaseModel):
    run_id: str
    workflow_name: str
    stage_id: str
    role_type: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    output_valid: bool = True
    quorum_score: float | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS traces (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT NOT NULL,
        workflow_name TEXT NOT NULL,
        stage_id    TEXT NOT NULL,
        role_type   TEXT NOT NULL,
        model       TEXT NOT NULL,
        input_tokens  INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        latency_ms  REAL DEFAULT 0.0,
        success     INTEGER NOT NULL DEFAULT 1,
        output_valid INTEGER NOT NULL DEFAULT 1,
        quorum_score REAL,
        timestamp   TEXT NOT NULL,
        inputs_json TEXT DEFAULT '{}',
        outputs_json TEXT DEFAULT '{}'
    )
"""


class TraceStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, trace: TraceRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO traces
                   (run_id, workflow_name, stage_id, role_type, model,
                    input_tokens, output_tokens, latency_ms, success, output_valid,
                    quorum_score, timestamp, inputs_json, outputs_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trace.run_id, trace.workflow_name, trace.stage_id,
                    trace.role_type, trace.model,
                    trace.input_tokens, trace.output_tokens, trace.latency_ms,
                    int(trace.success), int(trace.output_valid),
                    trace.quorum_score, trace.timestamp,
                    json.dumps(trace.inputs), json.dumps(trace.outputs),
                ),
            )
            await db.commit()

    async def query(
        self,
        workflow_name: str | None = None,
        min_quorum_score: float | None = None,
        limit: int = 100,
    ) -> list[TraceRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if workflow_name:
            conditions.append("workflow_name = ?")
            params.append(workflow_name)
        if min_quorum_score is not None:
            conditions.append("quorum_score >= ?")
            params.append(min_quorum_score)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM traces {where} ORDER BY timestamp DESC LIMIT ?", params
            )
            rows = await cursor.fetchall()
        return [
            TraceRecord(
                run_id=r["run_id"],
                workflow_name=r["workflow_name"],
                stage_id=r["stage_id"],
                role_type=r["role_type"],
                model=r["model"],
                input_tokens=r["input_tokens"] or 0,
                output_tokens=r["output_tokens"] or 0,
                latency_ms=r["latency_ms"] or 0.0,
                success=bool(r["success"]),
                output_valid=bool(r["output_valid"]),
                quorum_score=r["quorum_score"],
                timestamp=r["timestamp"],
                inputs=json.loads(r["inputs_json"] or "{}"),
                outputs=json.loads(r["outputs_json"] or "{}"),
            )
            for r in rows
        ]

    async def high_quality_traces(
        self, workflow_name: str, min_score: float = 0.85
    ) -> list[TraceRecord]:
        return await self.query(workflow_name=workflow_name, min_quorum_score=min_score)
```

- [ ] **Step 4: Run tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/state/test_traces.py -v
```

Expected: All 4 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/state/traces.py tests/state/test_traces.py
git commit -m "feat: SQLite trace store for run quality metrics and optimizer input"
```

---

## Task 2: Stage Model Updates + Engine Trace Instrumentation

**Files:**
- Modify: `armature/spec/models.py` (Stage class — add output_schema, subagent_spec)
- Modify: `armature/runtime/engine.py` (Harness — wire TraceStore, record LLM traces)
- Modify: `armature/nodes/llm.py` (LLMNode — return token usage in result metadata)
- Modify: `tests/runtime/test_engine.py` (add trace recording test)

- [ ] **Step 1: Write failing test for engine trace recording**

Add to `tests/runtime/test_engine.py`:

```python
# Add this import at top:
from armature.state.traces import TraceStore

async def test_harness_initializes_trace_store(tmp_path):
    spec = make_minimal_spec()
    harness = Harness(spec=spec, session_dir=tmp_path)
    assert hasattr(harness, "_traces")
    assert isinstance(harness._traces, TraceStore)
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/runtime/test_engine.py::test_harness_initializes_trace_store -v
```

Expected: `AttributeError: 'Harness' object has no attribute '_traces'`

- [ ] **Step 3: Add `output_schema` and `subagent_spec` to Stage in `armature/spec/models.py`**

In `armature/spec/models.py`, modify the `Stage` class — add two new optional fields after `condition`:

```python
class Stage(BaseModel):
    id: str
    role: Role | None = None
    depends_on: list[str] = Field(default_factory=list)
    adapter: str | None = None
    gate: str | None = None
    signature: Signature | None = None
    output_mode: OutputMode = OutputMode.TEXT
    on_fail: dict[str, Any] | None = None
    present: str | None = None
    condition: str | None = None
    output_schema: dict[str, Any] | None = None   # JSON Schema for GUIDED_JSON output
    subagent_spec: str | None = None              # Path to child workflow spec file
```

- [ ] **Step 4: Update `armature/runtime/engine.py` — wire TraceStore**

Add import at top of `armature/runtime/engine.py`:

```python
import time
from armature.state.traces import TraceStore, TraceRecord
```

In `Harness.__init__`, add after the `self._assembler = PromptAssembler()` line:

```python
        self._traces = TraceStore(base_dir / "traces.db")
```

Note: `TraceStore.init()` must be called before the first `record()`. Call it lazily on first use — add a private method `_ensure_traces()`:

```python
    async def _ensure_traces(self) -> None:
        if not hasattr(self, "_traces_initialized"):
            await self._traces.init()
            self._traces_initialized = True
```

- [ ] **Step 5: Instrument `_execute_stage` to record LLM traces**

In `Harness._execute_stage`, wrap the LLM branch to capture timing and record a trace. The full updated `_execute_stage` method:

```python
    async def _execute_stage(self, stage: Stage, context: dict[str, Any]) -> Any:
        await self._session.append(SessionEvent(type="stage_start", data={"stage": stage.id}))

        decision = await self._hooks.run_pre_stage(stage.id, context)
        if decision == HookDecision.BLOCK:
            raise PermissionError(f"Stage '{stage.id}' blocked by lifecycle hook")

        t0 = time.monotonic()

        if stage.gate == "human":
            node = HumanGateNode(stage=stage)
            result = await node.execute(context)
        elif stage.subagent_spec:
            from armature.nodes.subagent import SubagentNode
            node = SubagentNode(stage=stage, parent_dir=Path(self._spec_path).parent if hasattr(self, "_spec_path") else Path.cwd())
            result = await node.execute(context)
        elif stage.adapter:
            adapter = self._spec.adapters.get(stage.adapter)
            if adapter is None:
                raise ValueError(f"Adapter '{stage.adapter}' not defined in spec")
            node = ScriptNode(adapter=adapter)
            result = await node.execute(context)
        elif stage.role:
            node = LLMNode(
                stage=stage,
                tiers=self._spec.model_tiers,
                assembler=self._assembler,
                registry=self._registry,
            )
            result = await node.execute(context)
            # Record trace for LLM stages
            await self._ensure_traces()
            latency = (time.monotonic() - t0) * 1000
            output_valid = "_parse_error" not in result
            await self._traces.record(TraceRecord(
                run_id=self._run_id,
                workflow_name=self._spec.name,
                stage_id=stage.id,
                role_type=stage.role.type.value,
                model=node._resolve_model(),
                input_tokens=result.pop("_input_tokens", 0),
                output_tokens=result.pop("_output_tokens", 0),
                latency_ms=latency,
                success=True,
                output_valid=output_valid,
                inputs={k: str(v)[:200] for k, v in context.items()},
                outputs={k: str(v)[:200] for k, v in result.items()},
            ))
        else:
            raise ValueError(f"Stage '{stage.id}' has no role, adapter, or gate")

        await self._hooks.run_post_stage(stage.id, result, context)
        await self._session.append(SessionEvent(
            type="stage_complete", data={"stage": stage.id, "result": str(result)[:500]}
        ))
        return result
```

Also update `LLMNode.execute()` (in `armature/nodes/llm.py`) to inject token usage into the result dict so `_execute_stage` can extract it:

```python
    async def execute(self, context: dict[str, Any]) -> Any:
        role = self._stage.role
        tools = self._registry.descriptors() if self._registry else []
        system_prompt = self._assembler.build(role=role, tools=tools, context=context)
        model = self._resolve_model()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, default=str)},
        ]

        response = await litellm_completion(model=model, messages=messages)
        content = response.choices[0].message.content

        # Extract token usage for trace recording
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0

        if self._stage.output_mode.value in ("json", "guided_json"):
            try:
                result = json.loads(content)
                result["_input_tokens"] = input_tokens
                result["_output_tokens"] = output_tokens
                return result
            except json.JSONDecodeError:
                return {"raw": content, "_parse_error": True,
                        "_input_tokens": input_tokens, "_output_tokens": output_tokens}
        return {"content": content, "_input_tokens": input_tokens, "_output_tokens": output_tokens}
```

- [ ] **Step 6: Run all tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --tb=short -q
```

Expected: All tests pass (54+ passing, no regressions).

- [ ] **Step 7: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/spec/models.py armature/runtime/engine.py armature/nodes/llm.py tests/runtime/test_engine.py
git commit -m "feat: wire TraceStore into Harness engine, add output_schema/subagent_spec to Stage"
```

---

## Task 3: Guided JSON Decoding + Uncertainty-Aware Tier Escalation

**Files:**
- Modify: `armature/nodes/llm.py` (add response_format support + retry-with-escalation)
- Modify: `tests/nodes/test_llm.py` (add guided JSON and escalation tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/nodes/test_llm.py`:

```python
from unittest.mock import MagicMock

def make_litellm_response(content: str, input_tokens: int = 10, output_tokens: int = 5):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock()
    response.usage.prompt_tokens = input_tokens
    response.usage.completion_tokens = output_tokens
    return response

async def test_guided_json_passes_response_format():
    from armature.spec.models import OutputMode, Signature
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    stage.output_schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    captured_kwargs = {}
    async def mock_completion(**kwargs):
        captured_kwargs.update(kwargs)
        return make_litellm_response('{"score": 0.9}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert "response_format" in captured_kwargs
    assert result["score"] == pytest.approx(0.9)

async def test_tier_escalation_on_parse_failure():
    from armature.spec.models import OutputMode, ModelTierConfig
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    tiers = ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        medium=ModelTierConfig(provider="ollama", model="qwen2.5:14b"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0
    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response("not valid json")
        return make_litellm_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2  # first call failed, escalated to medium
    assert result.get("ok") is True
    assert "_parse_error" not in result

async def test_no_escalation_if_no_higher_tier():
    from armature.spec.models import OutputMode
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    # Only small tier configured — no escalation target
    tiers = ModelTiers(small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"))
    node = LLMNode(stage=stage, tiers=tiers)

    async def mock_completion(**kwargs):
        return make_litellm_response("not valid json")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result.get("_parse_error") is True  # gracefully returns parse error
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/nodes/test_llm.py -v -k "guided or escalation"
```

Expected: Tests fail (response_format not passed, no escalation logic).

- [ ] **Step 3: Update `armature/nodes/llm.py` — add guided JSON + escalation**

Replace the `execute` method with:

```python
    async def execute(self, context: dict[str, Any]) -> Any:
        role = self._stage.role
        tools = self._registry.descriptors() if self._registry else []
        system_prompt = self._assembler.build(role=role, tools=tools, context=context)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, default=str)},
        ]

        kwargs: dict[str, Any] = {"messages": messages}

        # Add response_format for guided JSON if schema is provided
        is_json_mode = self._stage.output_mode.value in ("json", "guided_json")
        if is_json_mode and self._stage.output_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{self._stage.id}_output",
                    "strict": True,
                    "schema": self._stage.output_schema,
                },
            }
        elif is_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        return await self._execute_with_escalation(kwargs, is_json_mode)

    async def _execute_with_escalation(
        self, base_kwargs: dict[str, Any], parse_as_json: bool
    ) -> Any:
        tier_name = self._stage.role.model_tier
        tried: set[str] = set()

        for attempt_tier in [tier_name] + _TIER_ORDER:
            if attempt_tier in tried:
                continue
            tier_config = getattr(self._tiers, attempt_tier, None)
            if tier_config is None:
                continue
            tried.add(attempt_tier)

            model = self._model_string(tier_config)
            response = await litellm_completion(model=model, **base_kwargs)
            content = response.choices[0].message.content

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0

            if parse_as_json:
                try:
                    result = json.loads(content)
                    result["_input_tokens"] = input_tokens
                    result["_output_tokens"] = output_tokens
                    return result
                except json.JSONDecodeError:
                    # Try escalating to next tier (only once per tier)
                    continue

            return {"content": content, "_input_tokens": input_tokens, "_output_tokens": output_tokens}

        # All tiers exhausted without a valid parse
        return {"raw": content, "_parse_error": True, "_input_tokens": 0, "_output_tokens": 0}

    def _model_string(self, tier_config) -> str:
        provider = tier_config.provider
        model = tier_config.model
        if provider == "ollama":
            return f"ollama/{model}"
        elif provider == "openrouter":
            return f"openrouter/{model}"
        return model
```

Also refactor `_resolve_model` to use `_model_string` for consistency:

```python
    def _resolve_model(self) -> str:
        tier_name = self._stage.role.model_tier
        tier_config = getattr(self._tiers, tier_name, None)
        if tier_config is None:
            for t in _TIER_ORDER:
                cfg = getattr(self._tiers, t, None)
                if cfg is not None:
                    tier_config = cfg
                    break
        if tier_config is None:
            raise ValueError(f"No model tier configured for '{tier_name}'")
        return self._model_string(tier_config)
```

- [ ] **Step 4: Run all LLM node tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/nodes/test_llm.py -v
```

Expected: All tests pass (3 original + 3 new = 6 total).

- [ ] **Step 5: Run full suite to check no regressions**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --tb=short -q
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/nodes/llm.py tests/nodes/test_llm.py
git commit -m "feat: guided JSON decoding via response_format + automatic tier escalation on parse failure"
```

---

## Task 4: Subagent Node (Fan-Out to Child Workflow)

**Files:**
- Create: `armature/nodes/subagent.py`
- Create: `tests/nodes/test_subagent.py`
- Create: `tests/fixtures/child-workflow.yaml`
- Modify: `armature/runtime/engine.py` (subagent dispatch branch already added in Task 2)

- [ ] **Step 1: Write failing tests**

Create `tests/fixtures/child-workflow.yaml`:

```yaml
name: child-workflow
version: "1.0"
description: Simple child workflow for subagent tests

adapters:
  greet:
    name: greet
    type: script
    cmd: "echo 'child says: {{greeting}}'"

stages:
  - id: respond
    adapter: greet
```

Create `tests/nodes/test_subagent.py`:

```python
import pytest
from pathlib import Path
from armature.nodes.subagent import SubagentNode
from armature.spec.models import Stage

FIXTURES = Path(__file__).parent.parent / "fixtures"

def make_subagent_stage() -> Stage:
    return Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
    )

async def test_subagent_runs_child_workflow(tmp_path):
    stage = make_subagent_stage()
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "hello"})
    assert "respond" in result
    assert result["respond"]["exit_code"] == 0
    assert "child says" in result["respond"]["stdout"]

async def test_subagent_passes_context_as_vars(tmp_path):
    stage = make_subagent_stage()
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "world"})
    assert "world" in result["respond"]["stdout"]

def test_subagent_raises_if_spec_missing(tmp_path):
    stage = Stage(id="bad", subagent_spec="/nonexistent/spec.yaml")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    import asyncio
    with pytest.raises(FileNotFoundError):
        asyncio.run(node.execute({}))
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/nodes/test_subagent.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write `armature/nodes/subagent.py`**

```python
from __future__ import annotations
from pathlib import Path
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Stage
from armature.spec.loader import load_spec


class SubagentNode(BaseNode):
    def __init__(self, stage: Stage, session_dir: Path | None = None):
        if not stage.subagent_spec:
            raise ValueError(f"Stage '{stage.id}' has no subagent_spec")
        self._stage = stage
        self._session_dir = session_dir

    async def execute(self, context: dict[str, Any]) -> Any:
        spec_path = Path(self._stage.subagent_spec)
        if not spec_path.exists():
            raise FileNotFoundError(f"Subagent spec not found: {spec_path}")

        # Import here to avoid circular import (engine imports subagent)
        from armature.runtime.engine import Harness

        child = Harness(
            spec=load_spec(spec_path, vars=context),
            session_dir=self._session_dir,
        )
        return await child.run(context)
```

- [ ] **Step 4: Run tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/nodes/test_subagent.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 5: Run full suite**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --tb=short -q
```

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/nodes/subagent.py tests/nodes/test_subagent.py tests/fixtures/child-workflow.yaml
git commit -m "feat: SubagentNode for fan-out to child workflow specs (harness component 4)"
```

---

## Task 5: FastAPI HTTP Service

**Files:**
- Create: `armature/service/__init__.py`
- Create: `armature/service/models.py`
- Create: `armature/service/app.py`
- Create: `tests/service/__init__.py`
- Create: `tests/service/test_app.py`
- Modify: `armature/cli.py` (add `serve` command)
- Modify: `pyproject.toml` (add `service` optional dep group)

- [ ] **Step 1: Add FastAPI to `pyproject.toml`**

Add optional dependency group to `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "httpx>=0.27",   # also used by test client
]
service = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
]
```

Install:

```bash
cd /Users/bryansparks/projects/armature && uv pip install -e ".[dev,service]"
```

- [ ] **Step 2: Write failing tests**

Create `tests/service/__init__.py` (empty).

Create `tests/service/test_app.py`:

```python
import pytest
from pathlib import Path
from httpx import AsyncClient, ASGITransport

FIXTURES = Path(__file__).parent.parent / "fixtures"

@pytest.fixture
def app():
    from armature.service.app import app
    return app

async def test_health_check(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

async def test_run_workflow(app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "http-test"},
            "session_dir": str(tmp_path),
        })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert "echo" in body["result"]
    assert body["result"]["echo"]["exit_code"] == 0

async def test_run_missing_spec(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": "/nonexistent/spec.yaml",
            "inputs": {},
        })
    assert response.status_code == 404
```

- [ ] **Step 3: Run to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/service/test_app.py -v
```

Expected: `ImportError`

- [ ] **Step 4: Write `armature/service/__init__.py`**

```python
```

(empty)

- [ ] **Step 5: Write `armature/service/models.py`**

```python
from __future__ import annotations
from typing import Any
from pydantic import BaseModel


class RunRequest(BaseModel):
    spec_path: str
    inputs: dict[str, Any] = {}
    session_dir: str | None = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
```

- [ ] **Step 6: Write `armature/service/app.py`**

```python
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, HTTPException
from armature.service.models import RunRequest, RunResponse
from armature.runtime.engine import Harness

app = FastAPI(title="Armature Service", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/run", response_model=RunResponse)
async def run_workflow(request: RunRequest):
    spec_path = Path(request.spec_path)
    if not spec_path.exists():
        raise HTTPException(status_code=404, detail=f"Spec not found: {request.spec_path}")

    try:
        session_dir = Path(request.session_dir) if request.session_dir else None
        harness = Harness.from_spec(spec_path, vars=request.inputs)
        if session_dir:
            # Re-create with explicit session_dir
            from armature.spec.loader import load_spec
            harness = Harness(spec=load_spec(spec_path, vars=request.inputs), session_dir=session_dir)
        result = await harness.run(request.inputs)
        return RunResponse(run_id=harness._run_id, status="complete", result=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
```

- [ ] **Step 7: Add `serve` command to `armature/cli.py`**

Add this command to `armature/cli.py` (after the existing `run` command):

```python
@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(8080, "--port", "-p", help="Bind port"),
):
    """Start the Armature HTTP service."""
    try:
        import uvicorn
        from armature.service.app import app as fastapi_app
    except ImportError:
        typer.echo("FastAPI/uvicorn not installed. Run: pip install 'armature[service]'", err=True)
        raise typer.Exit(1)
    typer.echo(f"Starting Armature service on {host}:{port}")
    uvicorn.run(fastapi_app, host=host, port=port)
```

- [ ] **Step 8: Run tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/service/test_app.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 9: Run full suite**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --tb=short -q
```

Expected: All tests pass.

- [ ] **Step 10: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/service/ armature/cli.py pyproject.toml tests/service/
git commit -m "feat: FastAPI HTTP service wrapper with /run and /health endpoints (Phase 2 service)"
```

---

## Task 6: Alembic Skill + Post-Run Hook

**Files:**
- Create: `armature/skills/alembic.py`
- Create: `tests/skills/__init__.py`
- Create: `tests/skills/test_alembic.py`
- Modify: `armature/registry/builtins.py` (register alembic.submit tool)

- [ ] **Step 1: Write failing tests**

Create `tests/skills/__init__.py` (empty).

Create `tests/skills/test_alembic.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from armature.skills.alembic import submit_trace, register_alembic_hook
from armature.state.traces import TraceRecord
from armature.hooks.lifecycle import HookRegistry, HookPhase

async def test_submit_trace_calls_api():
    trace = TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s",
        role_type="worker", model="qwen", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.9,
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.json.return_value = {"trace_id": "abc123"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await submit_trace({
            "trace": trace.model_dump(),
            "score": 0.9,
            "alembic_url": "http://localhost:8001",
        })

    assert result["submitted"] is True
    assert result["trace_id"] == "abc123"

async def test_submit_trace_raises_without_alembic_url():
    trace = TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s",
        role_type="worker", model="qwen", latency_ms=100,
        success=True, output_valid=True,
    )
    # No alembic_url and no quorum_score — should still attempt with default URL
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.json.return_value = {"trace_id": "xyz"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await submit_trace({"trace": trace.model_dump()})
    assert result["submitted"] is True

def test_register_alembic_hook_adds_post_stage_hook():
    registry = HookRegistry()
    register_alembic_hook(registry, threshold=0.8)
    # Hook should be registered for POST_STAGE
    assert len(registry._hooks[HookPhase.POST_STAGE]) == 1
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/skills/test_alembic.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write `armature/skills/alembic.py`**

```python
from __future__ import annotations
from typing import Any
import httpx
from armature.hooks.lifecycle import HookPhase, HookRegistry


async def submit_trace(args: dict[str, Any]) -> dict[str, Any]:
    """
    Submit a trace record to Alembic for SLM fine-tuning data.
    Args: { trace: dict (TraceRecord.model_dump()), score: float, alembic_url: str }
    Returns: { submitted: bool, trace_id: str }
    """
    alembic_url = args.get("alembic_url", "http://localhost:8001")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{alembic_url}/traces/submit",
            json={"trace": args["trace"], "score": args.get("score", 0.0)},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return {"submitted": True, "trace_id": data.get("trace_id", "")}


def register_alembic_hook(
    hook_registry: HookRegistry,
    threshold: float = 0.85,
    alembic_url: str = "http://localhost:8001",
) -> None:
    """
    Register a POST_STAGE hook that submits high-quality traces to Alembic.
    Only submits when quorum_score >= threshold (requires Quorum to have run).
    """
    async def _alembic_post_stage(phase, stage_id, result, ctx):
        score = ctx.get("_quorum_score")
        if score is not None and score >= threshold:
            trace_data = {k: v for k, v in result.items() if not k.startswith("_")}
            try:
                await submit_trace({
                    "trace": {"stage_id": stage_id, "outputs": trace_data, "run_id": ctx.get("run_id", "")},
                    "score": score,
                    "alembic_url": alembic_url,
                })
            except Exception:
                pass  # Never block execution on Alembic submission failure

    hook_registry.register(HookPhase.POST_STAGE, _alembic_post_stage)
```

- [ ] **Step 4: Register `alembic.submit` in `armature/registry/builtins.py`**

Add at the top of `builtins.py` (after existing skill imports):

```python
from armature.skills import alembic as _alembic_skill
```

Add at the end of `register_builtins()`:

```python
    registry.register(ToolDescriptor(
        name="alembic.submit",
        description="Submit a high-quality execution trace to Alembic for SLM fine-tuning",
        permission=PermissionLevel.NETWORK,
        handler=_alembic_skill.submit_trace,
        parameters={
            "trace": {"type": "object", "description": "TraceRecord as dict"},
            "score": {"type": "number", "optional": True},
            "alembic_url": {"type": "string", "optional": True},
        },
    ))
```

- [ ] **Step 5: Run tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/skills/test_alembic.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 6: Run full suite**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --tb=short -q
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/skills/alembic.py armature/registry/builtins.py tests/skills/
git commit -m "feat: Alembic trace submission skill + post-stage hook (harness flywheel loop 2)"
```

---

## ── PHASE 2B: Self-Improvement Loops ──

*Tasks 7–8 require real trace data and frontier model API access. Complete Phase 2A (Tasks 1–6) and run a few workflows before starting these.*

---

## Task 7: Meta-Harness Optimizer Workflow

**Files:**
- Create: `armature/optimizer/__init__.py`
- Create: `armature/optimizer/workflow.yaml`
- Create: `armature/optimizer/runner.py`
- Create: `tests/optimizer/__init__.py`
- Create: `tests/optimizer/test_optimizer.py`
- Modify: `armature/cli.py` (add `optimize` command)

- [ ] **Step 1: Write failing tests**

Create `tests/optimizer/__init__.py` (empty).

Create `tests/optimizer/test_optimizer.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from armature.optimizer.runner import OptimizerRunner, OptimizationResult

FIXTURES = Path(__file__).parent.parent / "fixtures"

def make_mock_harness_result(accept: bool = True):
    return {
        "analyze_traces": {"top_failure": "JSON parse errors", "failure_count": 5, "affected_stage": "worker1"},
        "propose_fix": {"proposed_diff": "- output_mode: text\n+ output_mode: guided_json", "rationale": "Add guided JSON", "confidence": 0.85},
        "evaluate_proposal": {"accept": accept, "score": 0.88 if accept else 0.3, "feedback": "Good change" if accept else "Too risky"},
    }

async def test_optimizer_returns_result(tmp_path):
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = make_mock_harness_result(accept=True)
        result = await runner.optimize()
    assert isinstance(result, OptimizationResult)
    assert result.accepted is True
    assert result.confidence == pytest.approx(0.85)

async def test_optimizer_returns_rejected_result(tmp_path):
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = make_mock_harness_result(accept=False)
        result = await runner.optimize()
    assert result.accepted is False

async def test_optimizer_no_traces_returns_none(tmp_path):
    runner = OptimizerRunner(
        target_spec_path=FIXTURES / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",  # empty DB, never init'd
    )
    result = await runner.optimize()
    assert result is None  # Not enough trace data
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/optimizer/test_optimizer.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Write `armature/optimizer/workflow.yaml`**

```yaml
name: harness-optimizer
version: "1.0"
description: "Meta-Harness — reads execution traces and proposes workflow spec improvements"

stages:
  - id: analyze_traces
    role:
      name: trace_analyst
      type: researcher
      model_tier: frontier
      description: |
        Analyze the execution traces provided in the context and identify the single most
        impactful failure pattern. Focus on: JSON parse errors (_parse_error in outputs),
        high latency stages (latency_ms > 2000), repeated validation failures, stages with
        success=false. Return structured analysis.
    output_mode: guided_json
    output_schema:
      type: object
      required: [top_failure, failure_count, affected_stage]
      properties:
        top_failure:
          type: string
          description: "Short description of the most common failure pattern"
        failure_count:
          type: integer
          description: "Number of traces showing this pattern"
        affected_stage:
          type: string
          description: "Stage ID most affected"

  - id: propose_fix
    depends_on: [analyze_traces]
    role:
      name: spec_optimizer
      type: orchestrator
      model_tier: frontier
      description: |
        Based on the failure analysis in context (top_failure, failure_count, affected_stage),
        propose a minimal change to the workflow YAML spec that directly addresses the failure.
        Output as a unified diff (--- original\n+++ proposed). Be conservative — change one
        thing at a time. If the failure is JSON parse errors, suggest adding output_mode: guided_json
        and an appropriate output_schema to the affected stage.
    output_mode: guided_json
    output_schema:
      type: object
      required: [proposed_diff, rationale, confidence]
      properties:
        proposed_diff:
          type: string
          description: "Unified diff format change to the YAML spec"
        rationale:
          type: string
          description: "Why this change addresses the failure"
        confidence:
          type: number
          description: "0.0-1.0 confidence the change will help"

  - id: evaluate_proposal
    depends_on: [propose_fix]
    role:
      name: spec_judge
      type: judge
      model_tier: frontier
      description: |
        Evaluate the proposed spec diff (proposed_diff, rationale, confidence) in context.
        Will this change address the failure without breaking existing functionality?
        Consider: Does the diff make sense given the failure? Is it minimal? Could it introduce
        regressions? Score the proposal objectively.
    output_mode: guided_json
    output_schema:
      type: object
      required: [accept, score, feedback]
      properties:
        accept:
          type: boolean
          description: "True if the proposal should be accepted"
        score:
          type: number
          description: "Quality score 0.0-1.0"
        feedback:
          type: string
          description: "Reasoning for accept/reject decision"
```

- [ ] **Step 4: Write `armature/optimizer/__init__.py`** (empty)

```python
```

- [ ] **Step 5: Write `armature/optimizer/runner.py`**

```python
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from pydantic import BaseModel
from armature.state.traces import TraceStore


class OptimizationResult(BaseModel):
    accepted: bool
    proposed_diff: str
    rationale: str
    confidence: float
    score: float
    feedback: str


class OptimizerRunner:
    MIN_TRACES = 5  # Don't optimize with fewer than 5 traces

    def __init__(
        self,
        target_spec_path: Path | str,
        trace_db_path: Path | str,
        optimizer_spec_path: Path | str | None = None,
    ):
        self._target_spec_path = Path(target_spec_path)
        self._trace_db_path = Path(trace_db_path)
        self._optimizer_spec_path = optimizer_spec_path or (
            Path(__file__).parent / "workflow.yaml"
        )

    async def optimize(self) -> OptimizationResult | None:
        traces = await self._load_traces()
        if len(traces) < self.MIN_TRACES:
            return None

        spec_yaml = self._target_spec_path.read_text(encoding="utf-8")
        traces_json = json.dumps([t.model_dump() for t in traces], default=str)

        workflow_result = await self._run_optimizer_workflow({
            "traces_json": traces_json,
            "spec_yaml": spec_yaml,
        })

        analyze = workflow_result.get("analyze_traces", {})
        propose = workflow_result.get("propose_fix", {})
        evaluate = workflow_result.get("evaluate_proposal", {})

        if not propose.get("proposed_diff") or not evaluate.get("accept") is not None:
            return None

        return OptimizationResult(
            accepted=bool(evaluate.get("accept", False)),
            proposed_diff=propose.get("proposed_diff", ""),
            rationale=propose.get("rationale", ""),
            confidence=float(propose.get("confidence", 0.0)),
            score=float(evaluate.get("score", 0.0)),
            feedback=evaluate.get("feedback", ""),
        )

    async def _load_traces(self):
        if not self._trace_db_path.exists():
            return []
        store = TraceStore(self._trace_db_path)
        await store.init()
        workflow_name = self._target_spec_path.stem
        return await store.query(workflow_name=workflow_name, limit=20)

    async def _run_optimizer_workflow(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from armature.runtime.engine import Harness
        harness = Harness.from_spec(self._optimizer_spec_path)
        return await harness.run(inputs)
```

- [ ] **Step 6: Add `optimize` command to `armature/cli.py`**

Add this command after the `serve` command in `armature/cli.py`:

```python
@app.command()
def optimize(
    spec: Path = typer.Argument(..., help="Path to the workflow spec to optimize"),
    trace_db: Path = typer.Option(
        Path("~/.armature/traces.db").expanduser(),
        "--traces", help="Path to trace database"
    ),
    apply: bool = typer.Option(False, "--apply", help="Apply the proposed diff if accepted"),
):
    """Run the Meta-Harness optimizer on a workflow spec."""
    if not spec.exists():
        typer.echo(f"Spec not found: {spec}", err=True)
        raise typer.Exit(1)

    from armature.optimizer.runner import OptimizerRunner
    import asyncio

    async def _run():
        runner = OptimizerRunner(target_spec_path=spec, trace_db_path=trace_db)
        return await runner.optimize()

    typer.echo(f"Analyzing traces for: {spec.name}")
    result = asyncio.run(_run())

    if result is None:
        typer.echo("Not enough trace data to optimize. Run more workflows first.")
        return

    typer.echo(f"\nOptimizer result (accepted={result.accepted}, score={result.score:.2f}):")
    typer.echo(f"Rationale: {result.rationale}")
    typer.echo(f"\nProposed diff:\n{result.proposed_diff}")

    if result.accepted and apply:
        typer.echo("\nApplying diff... (manual review recommended)")
        # Phase 3: auto-apply via patch
        typer.echo("Auto-apply not yet implemented. Review the diff and edit manually.")
```

- [ ] **Step 7: Run tests to verify PASS**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/optimizer/test_optimizer.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 8: Run full suite**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --tb=short -q
```

Expected: All tests pass.

- [ ] **Step 9: Commit**

```bash
cd /Users/bryansparks/projects/armature
git add armature/optimizer/ armature/cli.py tests/optimizer/
git commit -m "feat: Meta-Harness optimizer workflow — dogfood Armature to improve Armature specs (flywheel loop 1)"
```

---

## Task 8: Phase 2 Integration Test

**Files:**
- Create: `tests/fixtures/guided-json-workflow.yaml`
- Create: `tests/integration/test_phase2.py`

- [ ] **Step 1: Write guided-json-workflow fixture**

Create `tests/fixtures/guided-json-workflow.yaml`:

```yaml
name: guided-json-workflow
version: "1.0"
description: Tests guided JSON output mode end-to-end

adapters:
  classify:
    name: classify
    type: script
    cmd: "echo '{\"label\": \"positive\", \"confidence\": 0.95}'"

stages:
  - id: classify
    adapter: classify

  - id: report
    depends_on: [classify]
    adapter: summarize

adapters:
  classify:
    name: classify
    type: script
    cmd: "echo '{\"label\": \"positive\", \"confidence\": 0.95}'"
  summarize:
    name: summarize
    type: script
    cmd: "echo 'done'"
```

Wait — the YAML has duplicate adapters. Use the correct single-adapters version:

```yaml
name: guided-json-workflow
version: "1.0"
description: Tests script adapter output with JSON content

adapters:
  classify:
    name: classify
    type: script
    cmd: "echo '{\"label\": \"positive\", \"confidence\": 0.95}'"
  summarize:
    name: summarize
    type: script
    cmd: "echo 'classification complete'"

stages:
  - id: classify
    adapter: classify

  - id: report
    depends_on: [classify]
    adapter: summarize
```

- [ ] **Step 2: Write integration tests**

Create `tests/integration/test_phase2.py`:

```python
import pytest
from pathlib import Path
from armature.runtime.engine import Harness
from armature.state.traces import TraceStore

FIXTURES = Path(__file__).parent.parent / "fixtures"

async def test_trace_store_populated_after_run(tmp_path):
    """Engine records traces for LLM stages; script stages don't record (no LLM)."""
    harness = Harness.from_spec(
        FIXTURES / "echo-workflow.yaml",
        vars={"message": "trace-test"},
    )
    harness._session._path = tmp_path / "session.jsonl"
    # Redirect trace DB to tmp
    from armature.state.traces import TraceStore
    harness._traces = TraceStore(tmp_path / "traces.db")

    result = await harness.run({"message": "trace-test"})
    assert "echo" in result

async def test_subagent_fan_out_end_to_end(tmp_path):
    """Parent workflow fans out to a child workflow via SubagentNode."""
    from armature.spec.models import HarnessSpec, Stage
    from armature.spec.loader import load_spec

    # Parent spec with a subagent stage pointing to child-workflow.yaml
    parent_spec = HarnessSpec(
        name="parent-flow",
        version="1.0",
        stages=[
            Stage(
                id="child_run",
                subagent_spec=str(FIXTURES / "child-workflow.yaml"),
            )
        ],
    )
    harness = Harness(spec=parent_spec, session_dir=tmp_path)
    result = await harness.run({"greeting": "integration-test"})
    assert "child_run" in result
    assert "respond" in result["child_run"]
    assert "integration-test" in result["child_run"]["respond"]["stdout"]

async def test_service_run_via_http(tmp_path):
    """HTTP service runs a workflow and returns structured result."""
    from httpx import AsyncClient, ASGITransport
    from armature.service.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/run", json={
            "spec_path": str(FIXTURES / "echo-workflow.yaml"),
            "inputs": {"message": "phase2-integration"},
            "session_dir": str(tmp_path),
        })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete"
    assert body["result"]["echo"]["exit_code"] == 0
```

- [ ] **Step 3: Run integration tests**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest tests/integration/test_phase2.py -v
```

Expected: All 3 tests pass.

- [ ] **Step 4: Run full test suite with coverage**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/pytest --cov=armature --cov-report=term-missing -q
```

Expected: All tests pass. Coverage should be 80%+ on core modules.

- [ ] **Step 5: Final CLI smoke test**

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/armature --help
```

Expected: Shows run, serve, optimize commands.

```bash
cd /Users/bryansparks/projects/armature && .venv/bin/armature run tests/fixtures/echo-workflow.yaml --input message=smoke-test --dry-run
```

Expected: `Spec 'echo-workflow' loaded successfully (2 stages)`

- [ ] **Step 6: Final commit**

```bash
cd /Users/bryansparks/projects/armature
git add tests/fixtures/guided-json-workflow.yaml tests/integration/test_phase2.py
git commit -m "feat: Phase 2 integration tests — traces, subagent fan-out, HTTP service"
```

---

## Completion Checklist

Before declaring Phase 2 done:

- [ ] All Phase 1 tests still pass (no regressions)
- [ ] `armature serve` starts the FastAPI service on port 8080
- [ ] `POST /run` with `echo-workflow.yaml` returns 200 with correct result
- [ ] `armature optimize tests/fixtures/echo-workflow.yaml` runs without error (returns "not enough trace data" gracefully on first run)
- [ ] `SubagentNode` correctly fans out to `child-workflow.yaml` and merges results
- [ ] Tier escalation fires on JSON parse failure (verified by unit test)
- [ ] TraceStore records a trace for each LLM stage (verified by integration test)
- [ ] `register_alembic_hook(harness._hooks)` successfully adds POST_STAGE hook
- [ ] Full test suite passes: `pytest --cov=armature`
- [ ] 80%+ coverage maintained

---

*Phase 2B (Tasks 7–8) are production-ready once real traces exist from Phase 2A runs. The optimizer workflow.yaml requires frontier model API keys to run end-to-end.*

*Reference VISION.md at `/Users/bryansparks/projects/armature/VISION.md` for strategic context.*
