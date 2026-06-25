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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from armature.runtime.engine import Harness, _merge_carry_forward
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