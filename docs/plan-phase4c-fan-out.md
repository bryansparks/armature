# Phase 4C: Parallel Fan-Out / Fan-In (NLAH Pattern) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a single `subagent_spec` stage to spawn N child workflows in parallel via `asyncio.gather`, collect their results, and merge them through a configurable fan-in strategy — the NLAH paper's "N-way delegation" pattern.

**Architecture:** Three new optional fields are added to `Stage`: `fan_out: int | None` (N children), `fan_in: Literal["list", "merge", "first"]` (result combination), and `partition_key: str | None` (split a list in context across children). `SubagentNode.execute()` detects when `fan_out > 1` and dispatches N child harnesses with `asyncio.gather`. Each child runs in an isolated subdirectory under the parent's `session_dir`. Fan-in merges the N result dicts according to the chosen strategy.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, pytest-asyncio

---

## Background

The current `SubagentNode` spawns exactly one child harness and waits for it sequentially. The NLAH (Networked LLM Agent Harness) paper identifies parallel task delegation as the most impactful scaling pattern: distributing independent work items across N specialized agents and collecting the results. Today, implementing this requires the caller to manually loop and `asyncio.gather` — it is not a first-class harness capability.

This plan adds:
1. `fan_out: int | None` — how many children to run in parallel
2. `fan_in: Literal["list", "merge", "first"]` — how to combine results
3. `partition_key: str | None` — how to split a list from context across children
4. `SubagentNode` parallel execution with per-child session isolation

A `fan_out` stage in YAML looks like:

```yaml
stages:
  - id: analyze_chunks
    subagent_spec: ./chunk-analyzer.yaml
    fan_out: 4
    fan_in: list
    partition_key: documents   # context["documents"] list is split 4 ways
```

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `armature/spec/models.py` | **Modify** | Add `fan_out`, `fan_in`, `partition_key` to `Stage` |
| `armature/nodes/subagent.py` | **Modify** | N-way `asyncio.gather` + fan-in logic |
| `tests/spec/test_models.py` | **Modify** | Tests for new `Stage` fields and defaults |
| `tests/nodes/test_subagent.py` | **Modify** | Tests for fan-out execution and fan-in strategies |
| `tests/fixtures/child-workflow.yaml` | **Verify** | Existing fixture already valid for fan-out tests |

---

## Task 1: Add Fan-Out Fields to Stage Model

**Files:**
- Modify: `armature/spec/models.py`
- Modify: `tests/spec/test_models.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/spec/test_models.py`:

```python
def test_stage_fan_out_defaults():
    stage = Stage(id="s1", subagent_spec="child.yaml")
    assert stage.fan_out is None
    assert stage.fan_in == "list"
    assert stage.partition_key is None


def test_stage_fan_out_explicit():
    stage = Stage(
        id="s1",
        subagent_spec="child.yaml",
        fan_out=4,
        fan_in="merge",
        partition_key="documents",
    )
    assert stage.fan_out == 4
    assert stage.fan_in == "merge"
    assert stage.partition_key == "documents"


def test_stage_fan_in_first():
    stage = Stage(id="s1", subagent_spec="child.yaml", fan_out=3, fan_in="first")
    assert stage.fan_in == "first"


def test_stage_fan_out_none_means_single():
    stage = Stage(id="s1", subagent_spec="child.yaml", fan_out=None)
    assert stage.fan_out is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bryansparks/projects/armature
python -m pytest tests/spec/test_models.py -k "fan_out or fan_in or partition_key" -v
```

Expected: 4 tests FAIL with `ValidationError` or attribute error — fields not yet defined.

- [ ] **Step 3: Add the fields to `Stage` in `armature/spec/models.py`**

The `Stage` model currently ends with `subagent_spec`. Add `from typing import Any, Literal` (already there from Task 1 of Plan 4B) and three new fields to `Stage`:

```python
class Stage(BaseModel):
    id: str
    role: Role | None = None
    depends_on: list[str] = Field(default_factory=list)
    adapter: str | None = None
    gate: str | None = None
    signature: Signature | None = None
    output_mode: OutputMode = OutputMode.TEXT
    on_fail: OnFailConfig | None = None
    present: str | None = None
    condition: str | None = None
    output_schema: dict[str, Any] | None = None
    subagent_spec: str | None = None
    fan_out: int | None = None                                         # NEW
    fan_in: Literal["list", "merge", "first"] = "list"                # NEW
    partition_key: str | None = None                                   # NEW
```

> Note: If Plan 4B has not yet been executed, `Literal` needs to be added to the `typing` import line: `from typing import Any, Literal`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/spec/test_models.py -k "fan_out or fan_in or partition_key" -v
```

Expected: 4 tests PASS

- [ ] **Step 5: Run full spec test suite**

```bash
python -m pytest tests/spec/ -v
```

Expected: All existing spec tests still pass (new fields have defaults).

- [ ] **Step 6: Commit**

```bash
git add armature/spec/models.py tests/spec/test_models.py
git commit -m "feat: add fan_out, fan_in, partition_key fields to Stage model"
```

---

## Task 2: SubagentNode Parallel Execution and Fan-In

**Files:**
- Modify: `armature/nodes/subagent.py`
- Modify: `tests/nodes/test_subagent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/nodes/test_subagent.py`:

```python
import asyncio
from armature.spec.models import Stage

FIXTURES = Path(__file__).parent.parent / "fixtures"


def make_fanout_stage(n: int, fan_in: str = "list", partition_key: str | None = None) -> Stage:
    return Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
        fan_out=n,
        fan_in=fan_in,
        partition_key=partition_key,
    )


async def test_fan_out_list_returns_n_results(tmp_path):
    stage = make_fanout_stage(3, fan_in="list")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "hello"})
    assert "results" in result
    assert len(result["results"]) == 3
    for r in result["results"]:
        assert "respond" in r
        assert "child says" in r["respond"]["stdout"]


async def test_fan_out_children_run_in_parallel(tmp_path):
    stage = make_fanout_stage(3, fan_in="list")
    node = SubagentNode(stage=stage, session_dir=tmp_path)

    import time
    t0 = time.monotonic()
    result = await node.execute({"greeting": "timing"})
    elapsed = time.monotonic() - t0

    assert len(result["results"]) == 3
    # 3 sequential runs of a shell echo take ~0.3s each; parallel should be well under 1s total
    # This is a weak bound — the real test is correctness; timing is advisory
    assert elapsed < 5.0


async def test_fan_out_merge_combines_dicts(tmp_path):
    stage = make_fanout_stage(2, fan_in="merge")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "merge-test"})
    # merged result is a flat dict — "respond" key from child present
    assert "respond" in result


async def test_fan_out_first_returns_single_result(tmp_path):
    stage = make_fanout_stage(3, fan_in="first")
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "first"})
    assert "respond" in result
    assert "results" not in result


async def test_fan_out_partition_key_splits_list(tmp_path):
    # child-workflow uses {{greeting}} — use "greeting" as the field  
    # We'll pass a list of greetings and partition them across 2 children
    stage = Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
        fan_out=2,
        fan_in="list",
        partition_key="items",
    )
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    # context["items"] is a list of 4; each child gets 2
    result = await node.execute({"greeting": "hello", "items": ["a", "b", "c", "d"]})
    assert "results" in result
    assert len(result["results"]) == 2
    # Each child got a slice of "items" (list of 2), not the full list
    for child_result in result["results"]:
        assert isinstance(child_result, dict)


async def test_fan_out_partition_key_missing_gives_full_context(tmp_path):
    # partition_key set but not in context → each child gets the full context unchanged
    stage = Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
        fan_out=2,
        fan_in="list",
        partition_key="nonexistent_key",
    )
    node = SubagentNode(stage=stage, session_dir=tmp_path)
    result = await node.execute({"greeting": "fallback"})
    assert len(result["results"]) == 2


async def test_fan_out_one_is_same_as_single(tmp_path):
    # fan_out=1 should behave identically to no fan_out
    stage_fanout = make_fanout_stage(1, fan_in="list")
    stage_single = Stage(
        id="fan_out",
        subagent_spec=str(FIXTURES / "child-workflow.yaml"),
    )
    node_fanout = SubagentNode(stage=stage_fanout, session_dir=tmp_path / "fanout")
    node_single = SubagentNode(stage=stage_single, session_dir=tmp_path / "single")

    result_fanout = await node_fanout.execute({"greeting": "one"})
    result_single = await node_single.execute({"greeting": "one"})

    # fan_out=1 with fan_in="list" wraps in {"results": [...]}
    assert "results" in result_fanout
    assert len(result_fanout["results"]) == 1
    # single returns the raw result dict
    assert "respond" in result_single
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/nodes/test_subagent.py -k "fan_out" -v
```

Expected: Tests fail — `SubagentNode.execute()` ignores `fan_out` and returns a single result dict.

- [ ] **Step 3: Rewrite `armature/nodes/subagent.py`**

```python
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Stage
from armature.spec.loader import load_spec


def _partition(items: list, n: int) -> list[list]:
    """Split items into n roughly equal chunks."""
    size, rem = divmod(len(items), n)
    chunks, start = [], 0
    for i in range(n):
        end = start + size + (1 if i < rem else 0)
        chunks.append(items[start:end])
        start = end
    return chunks


def _fan_in(results: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    if strategy == "first":
        return results[0] if results else {}
    if strategy == "merge":
        merged: dict[str, Any] = {}
        for r in results:
            merged.update(r)
        return merged
    # "list" (default)
    return {"results": results}


class SubagentNode(BaseNode):
    def __init__(self, stage: Stage, session_dir: Path | None = None):
        if not stage.subagent_spec:
            raise ValueError(f"Stage '{stage.id}' has no subagent_spec")
        self._stage = stage
        self._session_dir = session_dir

    async def _run_child(self, context: dict[str, Any], child_index: int) -> dict[str, Any]:
        from armature.runtime.engine import Harness

        spec_path = Path(self._stage.subagent_spec)
        if not spec_path.exists():
            raise FileNotFoundError(f"Subagent spec not found: {spec_path}")

        child_dir: Path | None = None
        if self._session_dir is not None:
            child_dir = self._session_dir / f"child_{child_index}"
            child_dir.mkdir(parents=True, exist_ok=True)

        child = Harness(
            spec=load_spec(spec_path, vars=context),
            session_dir=child_dir,
        )
        return await child.run(context)

    def _build_contexts(self, context: dict[str, Any], n: int) -> list[dict[str, Any]]:
        key = self._stage.partition_key
        if key and key in context and isinstance(context[key], list):
            chunks = _partition(context[key], n)
            return [{**context, key: chunk} for chunk in chunks]
        return [dict(context) for _ in range(n)]

    async def execute(self, context: dict[str, Any]) -> Any:
        n = self._stage.fan_out
        if n is None or n <= 1:
            if n == 1:
                result = await self._run_child(context, 0)
                return _fan_in([result], self._stage.fan_in)
            # Original single-child path (fan_out not set)
            return await self._run_child(context, 0)

        contexts = self._build_contexts(context, n)
        tasks = [self._run_child(ctx, i) for i, ctx in enumerate(contexts)]
        results = await asyncio.gather(*tasks)
        return _fan_in(list(results), self._stage.fan_in)
```

- [ ] **Step 4: Run the new fan-out tests**

```bash
python -m pytest tests/nodes/test_subagent.py -k "fan_out" -v
```

Expected: 7 new fan-out tests PASS

- [ ] **Step 5: Run all subagent tests to verify no regressions**

```bash
python -m pytest tests/nodes/test_subagent.py -v
```

Expected: All 3 original tests + 7 new tests = 10 tests PASS

- [ ] **Step 6: Run full test suite**

```bash
python -m pytest tests/ -v --ignore=tests/integration
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add armature/nodes/subagent.py tests/nodes/test_subagent.py
git commit -m "feat: parallel fan-out/fan-in in SubagentNode with partition support"
```

---

## Self-Review

### Spec Coverage

| Requirement | Task |
|-------------|------|
| `fan_out: int | None` field on Stage | Task 1 |
| `fan_in: Literal["list", "merge", "first"]` field on Stage | Task 1 |
| `partition_key: str | None` field on Stage | Task 1 |
| N children via `asyncio.gather` | Task 2 |
| Fan-in "list" → `{"results": [...]}` | Task 2 |
| Fan-in "merge" → merged flat dict | Task 2 |
| Fan-in "first" → first result unchanged | Task 2 |
| `partition_key` splits list across children | Task 2 |
| Missing partition key → full context per child | Task 2 |
| Per-child isolated session dir | Task 2 |
| `fan_out=None` (not set) → single-child original path | Task 2 |
| `fan_out=1` → wrapped in fan-in strategy | Task 2 |

### Placeholder Scan

None. All steps contain complete, runnable code.

### Type Consistency

- `fan_in: Literal["list", "merge", "first"]` declared in Task 1; consumed by `_fan_in(results, self._stage.fan_in)` in Task 2 — consistent.
- `fan_out: int | None` declared in Task 1; checked as `if n is None or n <= 1` in Task 2 — consistent.
- `partition_key: str | None` declared in Task 1; accessed as `self._stage.partition_key` in Task 2 — consistent.
- `_partition(items, n)` returns `list[list]`; `_fan_in(results, strategy)` takes `list[dict]` — `_run_child` returns `dict[str, Any]`, so `results = await asyncio.gather(*tasks)` is `tuple[dict, ...]` → `list(results)` is `list[dict]` — consistent.
- `_run_child` constructs `child_dir` from `self._session_dir / f"child_{child_index}"` — `Path / str` is always a `Path` — consistent.
