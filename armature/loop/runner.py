"""The outer loop driver: runs a workflow repeatedly under a central budget.

Composition: ``LoopRunner.run`` calls ``Harness.run`` once per iteration
(via an injectable ``harness_factory``), accounts budget from the
``TraceStore``, applies the pure logic from ``armature.loop.logic``, and
writes one loop-summary trace row. No edits to the engine, spec models,
or validator.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from armature.runtime.engine import Harness
from armature.state.traces import TraceStore, TraceRecord

from armature.loop.carry import resolve_carry
from armature.loop.logic import build_iteration_inputs, decide_stop


@dataclass
class IterationRecord:
    iteration: int
    run_id: str
    llm_calls: int
    tokens: int
    latency_s: float


@dataclass
class LoopResult:
    loop_session_id: str
    workflow_name: str
    iterations: list[IterationRecord]
    final_result: dict
    stop_reason: str
    accumulated: dict
    error: str | None = None


async def _account_run(store: TraceStore, run_id: str | None) -> tuple[int, int]:
    """Count LLM-call rows and sum tokens for one run_id. None/missing -> (0, 0)."""
    if run_id is None:
        return 0, 0
    recs = await store.query_by_run(run_id)
    llm_calls = len(recs)
    tokens = sum(r.input_tokens + r.output_tokens for r in recs)
    return llm_calls, tokens


class LoopRunner:
    """Run a workflow back-to-back under a central budget until a stop condition.

    All loop policy is constructor args (mirrors the CLI flags). The
    ``harness_factory`` is injectable so tests run without real LLM calls;
    production passes ``None`` to use the real ``Harness``.
    """

    def __init__(
        self,
        spec,
        traces_db,
        *,
        inputs: dict | None = None,
        max_iterations: int = 10,
        max_llm_calls: int | None = None,
        max_wallclock: float | None = None,
        max_tokens: int | None = None,
        until: str | None = None,
        carry_forward: str = "*",
        inject_as: str = "prior_run",
        interval: float = 0.0,
        converge: bool = False,
        use_cache: bool = True,
        adapter_registry=None,
        harness_factory: Callable[[], Any] | None = None,
    ):
        from pathlib import Path
        self.spec = spec
        self.inputs = dict(inputs or {})
        self.max_iterations = max_iterations
        self.max_llm_calls = max_llm_calls
        self.max_wallclock = max_wallclock
        self.max_tokens = max_tokens
        self.until = until
        self.carry_forward = carry_forward
        self.inject_as = inject_as
        self.interval = interval
        self.converge = converge
        self.use_cache = use_cache
        self.adapter_registry = adapter_registry
        self.traces_db = Path(traces_db).expanduser() if traces_db else Path("~/.armature/traces.db").expanduser()
        self._harness_factory = harness_factory

    def _make_harness(self):
        if self._harness_factory is not None:
            return self._harness_factory()
        return Harness(
            spec=self.spec,
            traces_db=self.traces_db,
            use_cache=self.use_cache,
            adapter_registry=self.adapter_registry,
        )

    async def run(self) -> LoopResult:
        session_id = uuid.uuid4().hex[:8]
        store = TraceStore(self.traces_db)
        await store.init()
        budgets = {
            "max_llm_calls": self.max_llm_calls,
            "max_tokens": self.max_tokens,
            "max_wallclock": self.max_wallclock,
        }
        accumulated = {"llm_calls": 0, "tokens": 0, "wall_s": 0.0}
        iterations: list[IterationRecord] = []
        prev_result: dict | None = None
        final_result: dict = {}
        stop_reason = "max_iterations"
        error: str | None = None
        start = time.monotonic()

        for iteration_num in range(1, self.max_iterations + 1):
            carried = resolve_carry(prev_result, self.carry_forward)
            iter_inputs = build_iteration_inputs(
                self.inputs, iteration_num, self.max_iterations, carried, self.inject_as
            )

            iter_start = time.monotonic()
            harness = self._make_harness()
            try:
                result = await harness.run(iter_inputs)
            except Exception as exc:
                stop_reason = "error"
                error = f"{type(exc).__name__}: {exc}"
                break
            iter_latency = time.monotonic() - iter_start

            rid = harness._run_id
            calls, toks = await _account_run(store, rid)
            accumulated["llm_calls"] += calls
            accumulated["tokens"] += toks
            accumulated["wall_s"] = time.monotonic() - start
            iterations.append(IterationRecord(iteration_num, rid or "", calls, toks, iter_latency))
            final_result = result

            until_met = False
            if self.until is not None:
                try:
                    until_met = Harness._eval_until(self.until, result, iter_inputs)
                except Exception as exc:
                    stop_reason = "error"
                    error = f"until eval: {type(exc).__name__}: {exc}"
                    break

            stop = decide_stop(result, prev_result, until_met, self.converge, accumulated, budgets)
            if stop is not None:
                stop_reason = stop
                break

            prev_result = result
            if self.interval > 0 and iteration_num < self.max_iterations:
                await asyncio.sleep(self.interval)

        await self._write_summary(store, session_id, iterations, stop_reason, accumulated, final_result, error)

        return LoopResult(
            loop_session_id=session_id,
            workflow_name=self.spec.name,
            iterations=iterations,
            final_result=final_result,
            stop_reason=stop_reason,
            accumulated=accumulated,
            error=error,
        )

    async def _write_summary(self, store, session_id, iterations, stop_reason, accumulated, final_result, error):
        """Best-effort: write one loop-level summary trace row (no schema change)."""
        try:
            summary = TraceRecord(
                run_id=session_id,
                workflow_name=self.spec.name,
                stage_id="__loop__",
                role_type="orchestrator",
                model="loop-driver",
                input_tokens=0,
                output_tokens=0,
                latency_ms=accumulated["wall_s"] * 1000.0,
                success=(stop_reason != "error"),
                output_valid=True,
                quorum_score=None,
                loop_iteration=None,
                inputs={
                    "max_iterations": self.max_iterations,
                    "until": self.until,
                    "carry_forward": self.carry_forward,
                    "interval": self.interval,
                    "converge": self.converge,
                    "base_inputs": self.inputs,
                },
                outputs={
                    "iterations": [
                        {"iteration": ir.iteration, "run_id": ir.run_id,
                         "llm_calls": ir.llm_calls, "tokens": ir.tokens}
                        for ir in iterations
                    ],
                    "stop_reason": stop_reason,
                    "accumulated": accumulated,
                    "final_result": final_result,
                    "error": error,
                },
            )
            await store.record(summary)
        except Exception:
            pass  # summary row is best-effort; never fail the loop on it