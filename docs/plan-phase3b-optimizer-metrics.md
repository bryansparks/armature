# Phase 3B: Optimizer Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add IHR computation, A/B spec testing, and metric-driven optimization to the Armature optimizer.

**Architecture:** Three independent additions to two files — `armature/state/traces.py` gains IHR computation, `armature/optimizer/runner.py` gains A/B testing and metric-fn scoring. All three surface as new public methods or constructor params; existing interfaces are unchanged.

**Tech Stack:** Python 3.11+, aiosqlite, Pydantic v2, asyncio, pytest-asyncio

---

## Background

This plan implements three deferred items from `docs/deferred-research.md` (items 1, 3, 4 in priority order):

- **IHR (Implicit Harness Rating):** A formal composite quality metric per run, from the NLAH paper (arXiv:2603.25723). Gives the optimizer a single objective instead of unstructured trace analysis.
- **A/B spec testing:** Empirically compare original vs. proposed YAML specs by running both and comparing IHR scores. More reliable than LLM-as-judge alone.
- **Metric-driven optimization:** Accept a `metric_fn: Callable[[dict], float]` at construction time, score each trace's outputs with it, and pass `metric_mean` + `metric_scores_json` into the optimizer workflow's context so the `analyze_traces` stage has a programmatic signal.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `armature/state/traces.py` | Modify | Add `IhrResult` model, `query_by_run()`, `compute_ihr()` |
| `armature/optimizer/runner.py` | Modify | Add `ABTestResult`, `metric_fn` param, `a_b_test()`, `_run_one_and_score()` |
| `tests/state/test_traces.py` | Modify | Append IHR tests |
| `tests/optimizer/test_optimizer.py` | Modify | Append A/B and metric-fn tests |

---

## Task 1: IHR Computation

**Files:**
- Modify: `armature/state/traces.py`
- Modify: `tests/state/test_traces.py`

### IHR Formula

```
IHR = 0.40 * output_valid_rate
    + 0.30 * success_rate
    + 0.20 * avg_quorum_score   (default 0.5 when no quorum scores present)
    + 0.10 * latency_score      (latency_score = max(0.0, 1.0 - avg_latency_ms / 5000.0))
```

Range: 0.0–1.0. Higher is better.

---

- [ ] **Step 1: Write the failing tests**

Append to `tests/state/test_traces.py`:

```python
from armature.state.traces import IhrResult


async def _populate_run(store, run_id: str, n: int, **kwargs) -> None:
    for i in range(n):
        await store.record(TraceRecord(
            run_id=run_id,
            workflow_name=kwargs.get("workflow_name", "wf"),
            stage_id=f"s{i}",
            role_type="worker",
            model="test/model",
            latency_ms=kwargs.get("latency_ms", 500.0),
            success=kwargs.get("success", True),
            output_valid=kwargs.get("output_valid", True),
            quorum_score=kwargs.get("quorum_score", None),
        ))


async def test_compute_ihr_perfect(store):
    await _populate_run(store, "r1", 4,
        latency_ms=100.0, success=True, output_valid=True, quorum_score=1.0)
    result = await store.compute_ihr("r1")
    assert isinstance(result, IhrResult)
    assert result.run_id == "r1"
    assert result.ihr == pytest.approx(1.0, abs=1e-6)


async def test_compute_ihr_no_quorum_defaults_half(store):
    await _populate_run(store, "r2", 2,
        latency_ms=0.0, success=True, output_valid=True, quorum_score=None)
    result = await store.compute_ihr("r2")
    # latency_score=1.0, output_valid_rate=1.0, success_rate=1.0, quorum=0.5
    expected = 0.40 * 1.0 + 0.30 * 1.0 + 0.20 * 0.5 + 0.10 * 1.0
    assert result.ihr == pytest.approx(expected, abs=1e-6)


async def test_compute_ihr_partial_failures(store):
    await store.record(TraceRecord(
        run_id="r3", workflow_name="wf", stage_id="s1", role_type="worker",
        model="m", latency_ms=1000.0, success=True, output_valid=True, quorum_score=0.8))
    await store.record(TraceRecord(
        run_id="r3", workflow_name="wf", stage_id="s2", role_type="worker",
        model="m", latency_ms=3000.0, success=False, output_valid=False, quorum_score=0.4))
    result = await store.compute_ihr("r3")
    avg_latency = 2000.0
    latency_score = max(0.0, 1.0 - avg_latency / 5000.0)
    expected = (0.40 * 0.5    # output_valid_rate: 1/2
              + 0.30 * 0.5    # success_rate: 1/2
              + 0.20 * 0.6    # avg_quorum: (0.8+0.4)/2
              + 0.10 * latency_score)
    assert result.ihr == pytest.approx(expected, abs=1e-6)
    assert result.n_traces == 2


async def test_compute_ihr_unknown_run_returns_none(store):
    result = await store.compute_ihr("nonexistent")
    assert result is None


async def test_query_by_run_returns_only_that_run(store):
    await _populate_run(store, "runA", 3, workflow_name="wf")
    await _populate_run(store, "runB", 2, workflow_name="wf")
    records = await store.query_by_run("runA")
    assert len(records) == 3
    assert all(r.run_id == "runA" for r in records)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/state/test_traces.py -v -k "ihr or query_by_run"
```

Expected: FAIL — `ImportError: cannot import name 'IhrResult'`

- [ ] **Step 3: Implement `IhrResult`, `query_by_run()`, and `compute_ihr()` in `armature/state/traces.py`**

Add after the `TraceRecord` class (before `_CREATE_SQL`):

```python
class IhrResult(BaseModel):
    run_id: str
    ihr: float
    output_valid_rate: float
    success_rate: float
    avg_quorum_score: float
    latency_score: float
    n_traces: int
```

Add these two methods to `TraceStore` (after `high_quality_traces`):

```python
    async def query_by_run(self, run_id: str) -> list[TraceRecord]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM traces WHERE run_id = ? ORDER BY timestamp ASC", (run_id,)
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

    async def compute_ihr(self, run_id: str) -> "IhrResult | None":
        traces = await self.query_by_run(run_id)
        if not traces:
            return None

        n = len(traces)
        output_valid_rate = sum(1 for t in traces if t.output_valid) / n
        success_rate = sum(1 for t in traces if t.success) / n
        quorum_scores = [t.quorum_score for t in traces if t.quorum_score is not None]
        avg_quorum_score = sum(quorum_scores) / len(quorum_scores) if quorum_scores else 0.5
        avg_latency_ms = sum(t.latency_ms for t in traces) / n
        latency_score = max(0.0, 1.0 - avg_latency_ms / 5000.0)

        ihr = (
            0.40 * output_valid_rate
            + 0.30 * success_rate
            + 0.20 * avg_quorum_score
            + 0.10 * latency_score
        )
        return IhrResult(
            run_id=run_id,
            ihr=ihr,
            output_valid_rate=output_valid_rate,
            success_rate=success_rate,
            avg_quorum_score=avg_quorum_score,
            latency_score=latency_score,
            n_traces=n,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/state/test_traces.py -v
```

Expected: All tests PASS (including the 4 pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add armature/state/traces.py tests/state/test_traces.py
git commit -m "feat: add IhrResult, query_by_run, compute_ihr to TraceStore"
```

---

## Task 2: A/B Spec Testing

**Files:**
- Modify: `armature/optimizer/runner.py`
- Modify: `tests/optimizer/test_optimizer.py`

### Design

`a_b_test()` runs both specs N times on each input in `inputs_sample`, collects per-run IHR via `TraceStore.compute_ihr()`, then compares mean IHR. Winner is "proposed" if `delta > 0.01`, "original" if `delta < -0.01`, "tie" otherwise.

`_run_one_and_score()` is a private helper that creates a `Harness`, runs it, then reads IHR from its `TraceStore` using the run ID returned in `context["run_id"]`.

---

- [ ] **Step 1: Write the failing tests**

Append to `tests/optimizer/test_optimizer.py`:

```python
from armature.optimizer.runner import ABTestResult
from armature.state.traces import IhrResult


def make_ihr(run_id: str, ihr: float) -> IhrResult:
    return IhrResult(
        run_id=run_id,
        ihr=ihr,
        output_valid_rate=1.0,
        success_rate=1.0,
        avg_quorum_score=ihr,
        latency_score=1.0,
        n_traces=3,
    )


async def test_ab_test_proposed_wins(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    # original scores ~0.70, proposed ~0.90
    original_ihrs = [make_ihr(f"r{i}", 0.70) for i in range(3)]
    proposed_ihrs = [make_ihr(f"p{i}", 0.90) for i in range(3)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = original_ihrs + proposed_ihrs
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}, {"x": 3}],
            n_runs=1,
        )
    assert isinstance(result, ABTestResult)
    assert result.winner == "proposed"
    assert result.delta == pytest.approx(0.20, abs=1e-6)


async def test_ab_test_tie(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    all_ihrs = [make_ihr(f"r{i}", 0.80) for i in range(6)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = all_ihrs
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}, {"x": 3}],
            n_runs=1,
        )
    assert result.winner == "tie"


async def test_ab_test_original_wins(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
    )
    original_ihrs = [make_ihr(f"r{i}", 0.85) for i in range(3)]
    proposed_ihrs = [make_ihr(f"p{i}", 0.60) for i in range(3)]
    with patch.object(runner, "_run_one_and_score", new_callable=AsyncMock) as mock_score:
        mock_score.side_effect = original_ihrs + proposed_ihrs
        result = await runner.a_b_test(
            proposed_spec_path=fixtures / "echo-workflow.yaml",
            inputs_sample=[{"x": 1}, {"x": 2}, {"x": 3}],
            n_runs=1,
        )
    assert result.winner == "original"
    assert result.delta == pytest.approx(-0.25, abs=1e-6)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/optimizer/test_optimizer.py -v -k "ab_test"
```

Expected: FAIL — `ImportError: cannot import name 'ABTestResult'`

- [ ] **Step 3: Implement `ABTestResult`, `_run_one_and_score()`, and `a_b_test()` in `armature/optimizer/runner.py`**

Add `Callable` to the `typing` import at the top:

```python
from typing import Any, Callable
```

Add `ABTestResult` after `OptimizationResult`:

```python
class ABTestResult(BaseModel):
    original_ihr: float
    proposed_ihr: float
    delta: float
    winner: str  # "original" | "proposed" | "tie"
    n_runs: int
    n_inputs: int
```

Add these two methods to `OptimizerRunner` (after `_run_optimizer_workflow`):

```python
    async def _run_one_and_score(
        self, spec_path: Path, inputs: dict[str, Any]
    ) -> "IhrResult | None":
        from armature.runtime.engine import Harness
        from armature.spec.loader import load_spec
        from armature.state.traces import TraceStore

        spec = load_spec(spec_path)
        harness = Harness(spec=spec)
        result = await harness.run(inputs)
        run_id = result.get("run_id") or inputs.get("run_id")
        if not run_id:
            return None
        store = TraceStore(harness._traces._path)
        return await store.compute_ihr(run_id)

    async def a_b_test(
        self,
        proposed_spec_path: Path | str,
        inputs_sample: list[dict[str, Any]],
        n_runs: int = 5,
    ) -> ABTestResult:
        proposed_spec_path = Path(proposed_spec_path)

        async def score_spec(spec_path: Path) -> list[float]:
            scores: list[float] = []
            for _ in range(n_runs):
                for inp in inputs_sample:
                    ihr = await self._run_one_and_score(spec_path, inp)
                    if ihr is not None:
                        scores.append(ihr.ihr)
            return scores

        original_scores = await score_spec(self._target_spec_path)
        proposed_scores = await score_spec(proposed_spec_path)

        original_ihr = sum(original_scores) / len(original_scores) if original_scores else 0.0
        proposed_ihr = sum(proposed_scores) / len(proposed_scores) if proposed_scores else 0.0
        delta = proposed_ihr - original_ihr

        if delta > 0.01:
            winner = "proposed"
        elif delta < -0.01:
            winner = "original"
        else:
            winner = "tie"

        return ABTestResult(
            original_ihr=original_ihr,
            proposed_ihr=proposed_ihr,
            delta=delta,
            winner=winner,
            n_runs=n_runs,
            n_inputs=len(inputs_sample),
        )
```

Also add the `IhrResult` import at the top of the method (or add to file top-level imports inside the method body as shown — the import is inside `_run_one_and_score` to avoid circular imports at load time).

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/optimizer/test_optimizer.py -v
```

Expected: All tests PASS (including the 3 pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add armature/optimizer/runner.py tests/optimizer/test_optimizer.py
git commit -m "feat: add ABTestResult and a_b_test() to OptimizerRunner"
```

---

## Task 3: Metric-Driven Optimization

**Files:**
- Modify: `armature/optimizer/runner.py`
- Modify: `tests/optimizer/test_optimizer.py`

### Design

`OptimizerRunner.__init__` gains `metric_fn: Callable[[dict], float] | None = None`. In `optimize()`, after loading traces, each trace's `outputs` dict is scored with `metric_fn` (inside `try/except` — bad metric code must not crash the optimizer). The mean score and all per-trace scores are added to the workflow context as `metric_mean` (float) and `metric_scores_json` (JSON string). The `analyze_traces` stage then has these signals available alongside raw trace data.

---

- [ ] **Step 1: Write the failing tests**

Append to `tests/optimizer/test_optimizer.py`:

```python
async def test_metric_fn_scores_passed_to_workflow(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        metric_fn=lambda outputs: float(outputs.get("confidence", 0.0)),
    )
    # Fake traces with outputs that have confidence scores
    from armature.state.traces import TraceRecord
    fake_traces = [
        TraceRecord(
            run_id=f"r{i}", workflow_name="echo-workflow", stage_id="s",
            role_type="worker", model="m", latency_ms=100.0,
            success=True, output_valid=True,
            outputs={"confidence": 0.8 + i * 0.05},
        )
        for i in range(5)
    ]
    captured_inputs: list[dict] = []

    async def capture_workflow(inputs):
        captured_inputs.append(inputs)
        return make_mock_harness_result(accept=True)

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=fake_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          side_effect=capture_workflow):
            await runner.optimize()

    assert len(captured_inputs) == 1
    ctx = captured_inputs[0]
    assert "metric_mean" in ctx
    assert ctx["metric_mean"] == pytest.approx(0.9, abs=1e-6)  # (0.80+0.85+0.90+0.95+1.00)/5
    assert "metric_scores_json" in ctx
    scores = json.loads(ctx["metric_scores_json"])
    assert len(scores) == 5


async def test_metric_fn_none_omits_keys(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"
    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        # no metric_fn
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
    assert "metric_mean" not in ctx
    assert "metric_scores_json" not in ctx


async def test_metric_fn_exception_does_not_crash(tmp_path):
    fixtures = Path(__file__).parent.parent / "fixtures"

    def bad_metric(outputs):
        raise ValueError("metric blew up")

    runner = OptimizerRunner(
        target_spec_path=fixtures / "echo-workflow.yaml",
        trace_db_path=tmp_path / "traces.db",
        metric_fn=bad_metric,
    )
    from armature.state.traces import TraceRecord
    fake_traces = [
        TraceRecord(
            run_id="r0", workflow_name="echo-workflow", stage_id="s",
            role_type="worker", model="m", latency_ms=100.0,
            success=True, output_valid=True,
        )
        for _ in range(5)
    ]

    with patch.object(runner, "_load_traces", new_callable=AsyncMock, return_value=fake_traces):
        with patch.object(runner, "_run_optimizer_workflow", new_callable=AsyncMock,
                          return_value=make_mock_harness_result(accept=True)):
            result = await runner.optimize()

    assert result is not None  # optimizer completed despite metric_fn failure
```

Note: The test file already imports `json` and `patch`. If not, add them:

```python
import json
from unittest.mock import AsyncMock, patch
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/optimizer/test_optimizer.py -v -k "metric_fn"
```

Expected: FAIL — `TypeError: OptimizerRunner.__init__() got an unexpected keyword argument 'metric_fn'`

- [ ] **Step 3: Add `metric_fn` param and scoring logic to `armature/optimizer/runner.py`**

Update `__init__` signature:

```python
    def __init__(
        self,
        target_spec_path: Path | str,
        trace_db_path: Path | str,
        optimizer_spec_path: Path | str | None = None,
        metric_fn: Callable[[dict], float] | None = None,
    ):
        self._target_spec_path = Path(target_spec_path)
        self._trace_db_path = Path(trace_db_path)
        self._optimizer_spec_path = optimizer_spec_path or (
            Path(__file__).parent / "workflow.yaml"
        )
        self._metric_fn = metric_fn
```

Update the `optimize()` method to compute and inject metric scores. Replace the body of `optimize()` with:

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
                    outputs = t.outputs if hasattr(t, "outputs") else {}
                    scores.append(float(self._metric_fn(outputs)))
                except Exception:
                    pass
            if scores:
                workflow_inputs["metric_mean"] = sum(scores) / len(scores)
                workflow_inputs["metric_scores_json"] = json.dumps(scores)

        workflow_result = await self._run_optimizer_workflow(workflow_inputs)

        propose = workflow_result.get("propose_fix", {})
        evaluate = workflow_result.get("evaluate_proposal", {})

        if not propose.get("proposed_diff") or evaluate.get("accept") is None:
            return None

        return OptimizationResult(
            accepted=bool(evaluate.get("accept", False)),
            proposed_diff=propose.get("proposed_diff", ""),
            rationale=propose.get("rationale", ""),
            confidence=float(propose.get("confidence", 0.0)),
            score=float(evaluate.get("score", 0.0)),
            feedback=evaluate.get("feedback", ""),
        )
```

- [ ] **Step 4: Run the full test suite**

```bash
pytest tests/ -v
```

Expected: All tests PASS. Watch for no regressions in `test_optimizer_returns_result`, `test_optimizer_no_traces_returns_none`, or `test_record_and_query`.

- [ ] **Step 5: Commit**

```bash
git add armature/optimizer/runner.py tests/optimizer/test_optimizer.py
git commit -m "feat: add metric_fn scoring to OptimizerRunner.optimize()"
```

---

## Self-Review Checklist

- [x] **IHR formula** matches NLAH paper weighting (0.40/0.30/0.20/0.10)
- [x] **`query_by_run()`** tested independently (not just via `compute_ihr`)
- [x] **`compute_ihr()` returns `None`** for unknown run_id (not raises)
- [x] **A/B test `_run_one_and_score()`** tested via mock (avoids real LLM calls)
- [x] **A/B winner threshold** documented (±0.01)
- [x] **`metric_fn` failures** caught per-trace in `try/except`; optimizer continues
- [x] **`metric_fn=None`** produces no keys in workflow context (clean backward compat)
- [x] **No circular imports**: `IhrResult` imported inside `_run_one_and_score()` body
- [x] **`ABTestResult` and `IhrResult` importable** from their respective modules
- [x] **All existing tests** expected to still pass after each task
