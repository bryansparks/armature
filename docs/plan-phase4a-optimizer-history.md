# Phase 4A: Optimizer History (Multi-Iteration Meta-Harness) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the optimizer persistent memory of every prior proposal so it can reason causally across iterations — the core Meta-Harness insight.

**Architecture:** A new `ProposalStore` (SQLite-backed, mirrors `TraceStore` design) persists each proposed diff, its accept/reject outcome, and IHR delta. `OptimizerRunner` loads this history into the workflow context alongside traces, so `analyze_traces` can see what was already tried. A `run_loop(n_iterations)` method orchestrates multiple passes, each informed by the prior pass's outcome.

**Tech Stack:** Python 3.11+, aiosqlite, Pydantic v2, asyncio, pytest-asyncio

---

## Background

The current `OptimizerRunner.optimize()` is single-shot and stateless: it reads recent traces, runs the optimizer workflow once, and returns a proposed diff. The optimizer has no memory of what it previously proposed, which diffs were accepted, and what effect they had on IHR. Meta-Harness's key finding: full history access (not just scalar scores) improves optimization accuracy from 41% to 57%.

This plan adds:
1. `ProposalStore` — persistent history of every proposal, its rationale, acceptance, and IHR outcome
2. History injection into the optimizer workflow context
3. `run_loop(n_iterations)` — multi-iteration optimization that builds history across passes

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `armature/optimizer/history.py` | **Create** | `ProposalRecord` model + `ProposalStore` (SQLite CRUD) |
| `armature/optimizer/runner.py` | **Modify** | Accept `proposal_db_path`; inject history; record outcomes; add `run_loop()` |
| `armature/optimizer/workflow.yaml` | **Modify** | Update `analyze_traces` prompt to reference `proposal_history_json` |
| `tests/optimizer/test_history.py` | **Create** | Tests for `ProposalStore` init, record, load_history |
| `tests/optimizer/test_optimizer.py` | **Modify** | Tests for history injection in `optimize()` and `run_loop()` |

---

## Task 1: ProposalStore — Data Model and SQLite Backend

**Files:**
- Create: `armature/optimizer/history.py`
- Create: `tests/optimizer/test_history.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/optimizer/test_history.py`:

```python
import pytest
from pathlib import Path
from armature.optimizer.history import ProposalRecord, ProposalStore


@pytest.fixture
async def store(tmp_path):
    s = ProposalStore(tmp_path / "proposals.db")
    await s.init()
    return s


async def test_init_is_idempotent(tmp_path):
    store = ProposalStore(tmp_path / "proposals.db")
    await store.init()
    await store.init()   # must not raise


async def test_record_and_load(store):
    rec = ProposalRecord(
        proposal_id="abc12345",
        workflow_name="my-flow",
        proposed_diff="- foo\n+ bar",
        rationale="Fix JSON parse error",
        confidence=0.85,
        accepted=True,
        score=0.88,
        feedback="Good change",
    )
    await store.record(rec)
    history = await store.load_history("my-flow")
    assert len(history) == 1
    assert history[0].proposal_id == "abc12345"
    assert history[0].accepted is True
    assert history[0].confidence == pytest.approx(0.85)


async def test_load_history_filters_by_workflow(store):
    await store.record(ProposalRecord(
        proposal_id="r1", workflow_name="flow-a",
        proposed_diff="diff1", rationale="r", confidence=0.8,
        accepted=True, score=0.8, feedback="ok",
    ))
    await store.record(ProposalRecord(
        proposal_id="r2", workflow_name="flow-b",
        proposed_diff="diff2", rationale="r", confidence=0.7,
        accepted=False, score=0.3, feedback="bad",
    ))
    a = await store.load_history("flow-a")
    b = await store.load_history("flow-b")
    assert len(a) == 1 and a[0].proposal_id == "r1"
    assert len(b) == 1 and b[0].proposal_id == "r2"


async def test_load_history_returns_most_recent_first(store):
    for i in range(5):
        await store.record(ProposalRecord(
            proposal_id=f"r{i}", workflow_name="wf",
            proposed_diff=f"diff{i}", rationale="r", confidence=0.7,
            accepted=bool(i % 2), score=0.7, feedback="ok",
        ))
    history = await store.load_history("wf", limit=3)
    assert len(history) == 3
    assert history[0].proposal_id == "r4"  # most recent first


async def test_empty_db_returns_empty_history(store):
    history = await store.load_history("nonexistent")
    assert history == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/optimizer/test_history.py -v 2>&1 | head -15
```

Expected: `ImportError: cannot import name 'ProposalRecord'`

- [ ] **Step 3: Implement `armature/optimizer/history.py`**

```python
from __future__ import annotations
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field


class ProposalRecord(BaseModel):
    proposal_id: str
    workflow_name: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    proposed_diff: str
    rationale: str
    confidence: float
    accepted: bool
    score: float
    feedback: str


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS proposals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id   TEXT NOT NULL,
        workflow_name TEXT NOT NULL,
        timestamp     TEXT NOT NULL,
        proposed_diff TEXT NOT NULL,
        rationale     TEXT NOT NULL,
        confidence    REAL NOT NULL,
        accepted      INTEGER NOT NULL,
        score         REAL NOT NULL,
        feedback      TEXT NOT NULL
    )
"""


class ProposalStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, proposal: ProposalRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO proposals
                   (proposal_id, workflow_name, timestamp, proposed_diff,
                    rationale, confidence, accepted, score, feedback)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    proposal.proposal_id, proposal.workflow_name, proposal.timestamp,
                    proposal.proposed_diff, proposal.rationale, proposal.confidence,
                    int(proposal.accepted), proposal.score, proposal.feedback,
                ),
            )
            await db.commit()

    async def load_history(
        self, workflow_name: str, limit: int = 20
    ) -> list[ProposalRecord]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM proposals WHERE workflow_name = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (workflow_name, limit),
            )
            rows = await cursor.fetchall()
        return [
            ProposalRecord(
                proposal_id=r["proposal_id"],
                workflow_name=r["workflow_name"],
                timestamp=r["timestamp"],
                proposed_diff=r["proposed_diff"],
                rationale=r["rationale"],
                confidence=r["confidence"],
                accepted=bool(r["accepted"]),
                score=r["score"],
                feedback=r["feedback"],
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/optimizer/test_history.py -v 2>&1
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/bryansparks/projects/armature && git add armature/optimizer/history.py tests/optimizer/test_history.py && git commit -m "feat: add ProposalStore for optimizer proposal history persistence"
```

---

## Task 2: Inject Proposal History into `OptimizerRunner`

**Files:**
- Modify: `armature/optimizer/runner.py` (lines 34–46, `__init__`; lines 48–90, `optimize()`)
- Modify: `tests/optimizer/test_optimizer.py` (append 3 tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/optimizer/test_optimizer.py`:

```python
import json as _json
from armature.optimizer.history import ProposalRecord, ProposalStore


async def test_optimize_injects_proposal_history(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    proposal_db = tmp_path / "proposals.db"

    # Pre-populate history with two prior proposals
    store = ProposalStore(proposal_db)
    await store.init()
    await store.record(ProposalRecord(
        proposal_id="old1", workflow_name="echo-workflow",
        proposed_diff="- text\n+ guided_json", rationale="Fix parse errors",
        confidence=0.9, accepted=True, score=0.88, feedback="Improved output validity",
    ))
    await store.record(ProposalRecord(
        proposal_id="old2", workflow_name="echo-workflow",
        proposed_diff="- model: small\n+ model: medium", rationale="Improve quality",
        confidence=0.6, accepted=False, score=0.3, feedback="Introduced regression",
    ))

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        proposal_db_path=proposal_db,
    )
    mock_traces = [object()] * 5
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            await runner.optimize()

    ctx = captured_inputs[0]
    assert "proposal_history_json" in ctx
    history = _json.loads(ctx["proposal_history_json"])
    assert len(history) == 2
    # Most recent first — old2 was recorded after old1
    assert history[0]["proposal_id"] == "old2"
    assert history[1]["proposal_id"] == "old1"


async def test_optimize_records_result_to_proposal_store(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    proposal_db = tmp_path / "proposals.db"

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        proposal_db_path=proposal_db,
    )
    mock_traces = [object()] * 5

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          return_value=make_mock_harness_result(accept=True)):
            result = await runner.optimize()

    assert result is not None
    store = ProposalStore(proposal_db)
    history = await store.load_history("echo-workflow")
    assert len(history) == 1
    assert history[0].accepted is True
    assert history[0].proposed_diff == result.proposed_diff


async def test_optimize_no_proposal_db_still_works(tmp_path):
    """proposal_db_path is optional — existing behavior unchanged."""
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        # no proposal_db_path
    )
    mock_traces = [object()] * 5
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=mock_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            result = await runner.optimize()

    assert result is not None
    assert "proposal_history_json" not in captured_inputs[0]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/optimizer/test_optimizer.py -v -k "proposal" 2>&1 | head -20
```

Expected: `TypeError: OptimizerRunner.__init__() got an unexpected keyword argument 'proposal_db_path'`

- [ ] **Step 3: Modify `armature/optimizer/runner.py`**

Add `import uuid` to the top-level imports.

Update `__init__` to accept `proposal_db_path`:

```python
    def __init__(
        self,
        target_spec_path: Path | str,
        trace_db_path: Path | str,
        optimizer_spec_path: Path | str | None = None,
        metric_fn: Callable[[dict[str, Any]], float] | None = None,
        proposal_db_path: Path | str | None = None,
    ):
        self._target_spec_path = Path(target_spec_path)
        self._trace_db_path = Path(trace_db_path)
        self._optimizer_spec_path = optimizer_spec_path or (
            Path(__file__).parent / "workflow.yaml"
        )
        self._metric_fn = metric_fn
        self._proposal_db_path = Path(proposal_db_path) if proposal_db_path else None
```

Update `optimize()` to load history before running and record the result after:

```python
    async def optimize(self) -> OptimizationResult | None:
        traces = await self._load_traces()
        if len(traces) < self.MIN_TRACES:
            return None

        spec_yaml = self._target_spec_path.read_text(encoding="utf-8")
        traces_json = json.dumps(
            [t.model_dump() if hasattr(t, "model_dump") else {} for t in traces],
            default=str,
        )

        workflow_inputs: dict[str, Any] = {
            "traces_json": traces_json,
            "spec_yaml": spec_yaml,
        }

        if self._metric_fn is not None:
            scores: list[float] = []
            for t in traces:
                try:
                    scores.append(float(self._metric_fn(t.outputs)))
                except Exception:
                    pass
            if scores:
                workflow_inputs["metric_mean"] = sum(scores) / len(scores)
                workflow_inputs["metric_scores_json"] = json.dumps(scores)

        if self._proposal_db_path is not None:
            from armature.optimizer.history import ProposalStore
            proposal_store = ProposalStore(self._proposal_db_path)
            await proposal_store.init()
            history = await proposal_store.load_history(self._target_spec_path.stem)
            if history:
                workflow_inputs["proposal_history_json"] = json.dumps(
                    [p.model_dump() for p in history], default=str
                )

        workflow_result = await self._run_optimizer_workflow(workflow_inputs)

        propose = workflow_result.get("propose_fix", {})
        evaluate = workflow_result.get("evaluate_proposal", {})

        if not propose.get("proposed_diff") or evaluate.get("accept") is None:
            return None

        result = OptimizationResult(
            accepted=bool(evaluate.get("accept", False)),
            proposed_diff=propose.get("proposed_diff", ""),
            rationale=propose.get("rationale", ""),
            confidence=float(propose.get("confidence", 0.0)),
            score=float(evaluate.get("score", 0.0)),
            feedback=evaluate.get("feedback", ""),
        )

        if self._proposal_db_path is not None:
            from armature.optimizer.history import ProposalStore, ProposalRecord
            proposal_store = ProposalStore(self._proposal_db_path)
            await proposal_store.init()
            await proposal_store.record(ProposalRecord(
                proposal_id=str(uuid.uuid4())[:8],
                workflow_name=self._target_spec_path.stem,
                proposed_diff=result.proposed_diff,
                rationale=result.rationale,
                confidence=result.confidence,
                accepted=result.accepted,
                score=result.score,
                feedback=result.feedback,
            ))

        return result
```

Also add `import uuid` at the top of the file alongside the other stdlib imports.

- [ ] **Step 4: Run all optimizer tests**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/optimizer/test_optimizer.py -v 2>&1
```

Expected: All 14 tests PASS (11 previous + 3 new).

- [ ] **Step 5: Commit**

```bash
cd /Users/bryansparks/projects/armature && git add armature/optimizer/runner.py tests/optimizer/test_optimizer.py && git commit -m "feat: inject proposal history into optimizer workflow context, record outcomes"
```

---

## Task 3: `run_loop()` — Multi-Iteration Optimization

**Files:**
- Modify: `armature/optimizer/runner.py` (add `run_loop()` method)
- Modify: `tests/optimizer/test_optimizer.py` (append 3 tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/optimizer/test_optimizer.py`:

```python
from armature.optimizer.runner import LoopResult


async def test_run_loop_runs_n_iterations(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        proposal_db_path=tmp_path / "proposals.db",
    )
    mock_traces = [object()] * 5
    call_count = 0

    async def mock_optimize():
        nonlocal call_count
        call_count += 1
        return OptimizationResult(
            accepted=True,
            proposed_diff=f"diff-{call_count}",
            rationale="test",
            confidence=0.8,
            score=0.8,
            feedback="ok",
        )

    with patch.object(runner, "optimize", new_callable=AsyncMock, side_effect=mock_optimize):
        loop_result = await runner.run_loop(n_iterations=3)

    assert call_count == 3
    assert isinstance(loop_result, LoopResult)
    assert len(loop_result.iterations) == 3
    assert loop_result.accepted_count == 3


async def test_run_loop_stops_early_on_none(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    results = [
        OptimizationResult(accepted=True, proposed_diff="d", rationale="r",
                           confidence=0.8, score=0.8, feedback="ok"),
        None,   # not enough traces on second pass — stop
        OptimizationResult(accepted=True, proposed_diff="d2", rationale="r",
                           confidence=0.8, score=0.8, feedback="ok"),
    ]
    with patch.object(runner, "optimize", new_callable=AsyncMock, side_effect=results):
        loop_result = await runner.run_loop(n_iterations=3)

    assert len(loop_result.iterations) == 2   # stopped after None
    assert loop_result.accepted_count == 1


async def test_run_loop_zero_iterations(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    with patch.object(runner, "optimize", new_callable=AsyncMock) as mock_opt:
        loop_result = await runner.run_loop(n_iterations=0)
    mock_opt.assert_not_called()
    assert loop_result.iterations == []
    assert loop_result.accepted_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/optimizer/test_optimizer.py -v -k "run_loop" 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'LoopResult'`

- [ ] **Step 3: Add `LoopResult` model and `run_loop()` to `armature/optimizer/runner.py`**

Add after `ABTestResult`:

```python
class LoopResult(BaseModel):
    iterations: list[OptimizationResult | None]
    accepted_count: int
    rejected_count: int
    n_iterations_run: int
```

Add after `a_b_test()`:

```python
    async def run_loop(self, n_iterations: int = 5) -> LoopResult:
        iterations: list[OptimizationResult | None] = []
        for _ in range(n_iterations):
            result = await self.optimize()
            if result is None:
                break   # not enough traces — no point continuing
            iterations.append(result)

        accepted = sum(1 for r in iterations if r is not None and r.accepted)
        rejected = sum(1 for r in iterations if r is not None and not r.accepted)
        return LoopResult(
            iterations=iterations,
            accepted_count=accepted,
            rejected_count=rejected,
            n_iterations_run=len(iterations),
        )
```

- [ ] **Step 4: Run all optimizer tests**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/optimizer/ -v 2>&1
```

Expected: All 17 tests PASS (across `test_optimizer.py` + `test_history.py`).

- [ ] **Step 5: Commit**

```bash
cd /Users/bryansparks/projects/armature && git add armature/optimizer/runner.py tests/optimizer/test_optimizer.py && git commit -m "feat: add LoopResult and run_loop() for multi-iteration optimization"
```

---

## Task 4: Update Optimizer Workflow to Use Proposal History

**Files:**
- Modify: `armature/optimizer/workflow.yaml` (update `analyze_traces` description)

- [ ] **Step 1: Update the `analyze_traces` stage description**

The current `analyze_traces` stage description does not mention proposal history. Update it to:

```yaml
  - id: analyze_traces
    role:
      name: trace_analyst
      type: researcher
      model_tier: frontier
      description: |
        Analyze the execution traces and proposal history provided in the context.

        Context includes:
        - traces_json: Recent execution traces (stage outcomes, latencies, parse errors)
        - proposal_history_json (if present): Prior optimization proposals, whether each
          was accepted, and its quality score. Use this to avoid re-proposing changes that
          were already tried and rejected, and to build on changes that worked.

        Identify the single most impactful failure pattern NOT already addressed by a
        prior accepted proposal. Focus on: JSON parse errors (_parse_error in outputs),
        high latency stages (latency_ms > 2000), repeated validation failures, stages with
        success=false.

        If proposal_history_json shows a prior accepted diff for the same failure type,
        look for the next most impactful issue instead.

        Return structured analysis.
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
```

- [ ] **Step 2: Verify the YAML is valid**

```bash
cd /Users/bryansparks/projects/armature && python -c "
from armature.spec.loader import load_spec
spec = load_spec('armature/optimizer/workflow.yaml')
print('OK —', spec.name, len(spec.stages), 'stages')
"
```

Expected: `OK — harness-optimizer 3 stages`

- [ ] **Step 3: Run the full test suite**

```bash
cd /Users/bryansparks/projects/armature && python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests PASS, 0 failures.

- [ ] **Step 4: Commit**

```bash
cd /Users/bryansparks/projects/armature && git add armature/optimizer/workflow.yaml && git commit -m "feat: update analyze_traces to use proposal history for causal optimization"
```

---

## Self-Review Checklist

- [x] `ProposalStore` mirrors `TraceStore` design (SQLite, aiosqlite, idempotent `init`)
- [x] `proposal_db_path=None` leaves existing `optimize()` behavior unchanged
- [x] History is injected as `proposal_history_json` only when non-empty
- [x] Each `optimize()` call records its result to the store (win or loss)
- [x] `run_loop()` stops early when `optimize()` returns `None` (not enough traces)
- [x] `LoopResult` counts accepted and rejected proposals separately
- [x] Workflow YAML updated to explain causal reasoning over history
- [x] All existing optimizer tests still pass (no breaking changes)
