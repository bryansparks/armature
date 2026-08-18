"""The ``armature tune`` facade: a budgeted closed loop that runs the cheap
``improve`` engine by default and escalates to the expensive ``optimize`` engine
only when improve stalls.

Each iteration: run the workflow (fresh traces) → run ``improve`` (cheap, one
medium-tier completion) → read recent ``source="improve"`` history from the
unified ``ImprovementStore`` and run :func:`detect_stall` → if stalled and under
the escalation cap, run ``optimize`` (expensive meta-workflow) and, when it
accepts and ``auto_apply`` is set, patch the spec via
``OptimizerRunner.apply_diff``. Stops on HQS≥target (converged), healthy
(nothing to improve), budget exhaustion, max iterations, or the escalation cap.

The three sub-engines are injectable factories so tests run without real LLM
calls; production passes ``None`` to use the real ``Harness`` /
``SelfImproveRunner`` / ``OptimizerRunner``. Budget accounting reuses
``loop``'s ``_account_run`` (LLM-call rows + token sum per run_id) and the
``accumulated``-dict pattern from ``LoopRunner``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from armature.loop.runner import _account_run
from armature.optimizer.runner import OptimizerRunner
from armature.state.improvement_store import ImprovementStore
from armature.state.traces import TraceStore
from armature.tune.stall import StallVerdict, detect_stall


@dataclass
class TuneIteration:
    iteration: int
    run_hqs: float | None
    improve_applied: bool
    improve_escalated_oscillation: bool
    stall: StallVerdict | None
    optimize_ran: bool
    optimize_accepted: bool | None
    optimize_applied: bool
    llm_calls: int
    tokens: int
    wall_s: float
    stop_reason: str | None = None


@dataclass
class TuneResult:
    iterations: list[TuneIteration] = field(default_factory=list)
    stop_reason: str = "max_iterations"
    final_hqs: float | None = None
    escalations: int = 0
    llm_calls: int = 0
    tokens: int = 0
    wall_s: float = 0.0
    error: str | None = None


class TuneRunner:
    """Run a budgeted improve→optimize closed loop with auto-escalation.

    All policy is constructor args (mirrors the CLI flags). The three factories
    are injectable; production passes ``None`` for each to use the real engines.
    """

    def __init__(
        self,
        spec_path: Path | str,
        *,
        inputs: dict | None = None,
        trace_db: Path | str | None = None,
        improvement_db: Path | str | None = None,
        target_hqs: float = 0.90,
        min_traces: int = 3,
        drift_threshold: float = 0.5,
        stall_window: int = 3,
        max_escalations: int = 2,
        auto_apply: bool = True,
        max_iterations: int = 10,
        max_llm_calls: int | None = None,
        max_wallclock: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        use_cache: bool = True,
        harness_factory: Callable[[Path], Any] | None = None,
        improve_factory: Callable[[Path], Any] | None = None,
        optimize_factory: Callable[[Path, Path], Any] | None = None,
    ):
        self.spec_path = Path(spec_path)
        self.inputs = dict(inputs or {})
        self.trace_db = Path(trace_db).expanduser() if trace_db else Path("~/.armature/traces.db").expanduser()
        self.improvement_db = Path(improvement_db).expanduser() if improvement_db else None
        self.target_hqs = target_hqs
        self.min_traces = min_traces
        self.drift_threshold = drift_threshold
        self.stall_window = stall_window
        self.max_escalations = max_escalations
        self.auto_apply = auto_apply
        self.max_iterations = max_iterations
        self.max_llm_calls = max_llm_calls
        self.max_wallclock = max_wallclock
        self.max_tokens = max_tokens
        self.model = model
        self.use_cache = use_cache

        self._harness_factory = harness_factory or self._default_harness_factory()
        self._improve_factory = improve_factory or self._default_improve_factory()
        self._optimize_factory = optimize_factory or self._default_optimize_factory()

    def _default_harness_factory(self):
        def make(spec_path):
            from armature.spec.loader import load_spec
            from armature.runtime.engine import Harness
            return Harness(spec=load_spec(spec_path), traces_db=self.trace_db, use_cache=self.use_cache)
        return make

    def _default_improve_factory(self):
        def make(spec_path):
            from armature.synthesis.improve import SelfImproveRunner
            return SelfImproveRunner(
                spec_path, self.trace_db,
                target_hqs=self.target_hqs, min_traces=self.min_traces,
                drift_threshold=self.drift_threshold, auto_apply=self.auto_apply,
                improvement_db_path=self.improvement_db, model=self.model,
            )
        return make

    def _default_optimize_factory(self):
        def make(spec_path, trace_db):
            return OptimizerRunner(
                target_spec_path=spec_path, trace_db_path=trace_db,
                improvement_db_path=self.improvement_db, model_override=self.model,
            )
        return make

    async def run(self) -> TuneResult:
        trace_store = TraceStore(self.trace_db)
        await trace_store.init()
        imp_store: ImprovementStore | None = None
        if self.improvement_db is not None:
            imp_store = ImprovementStore(self.improvement_db)
            await imp_store.init()

        accumulated = {"llm_calls": 0, "tokens": 0, "wall_s": 0.0}
        iterations: list[TuneIteration] = []
        escalations = 0
        stop_reason = "max_iterations"
        final_hqs: float | None = None
        error: str | None = None
        stem = self.spec_path.stem
        start = time.monotonic()

        for it in range(1, self.max_iterations + 1):
            # 1. run the workflow → fresh traces for this iteration
            harness = self._harness_factory(self.spec_path)
            try:
                await harness.run(self.inputs)
            except Exception as exc:  # noqa: BLE001
                stop_reason = "error"
                error = f"{type(exc).__name__}: {exc}"
                break
            rid = getattr(harness, "_run_id", None)
            calls, toks = await _account_run(trace_store, rid)
            accumulated["llm_calls"] += calls
            accumulated["tokens"] += toks
            accumulated["wall_s"] = time.monotonic() - start

            # 2. improve (cheap)
            report = await self._improve_factory(self.spec_path).analyze()
            run_hqs = report.hqs_before
            final_hqs = run_hqs

            # 3. converged / healthy
            stall_verdict: StallVerdict | None = None
            optimize_ran = False
            optimize_accepted: bool | None = None
            optimize_applied = False
            stop: str | None = None

            if run_hqs is not None and run_hqs >= self.target_hqs:
                stop = "converged"
            elif (
                not report.needs_improvement
                and report.proposed_spec is None
                and report.n_traces >= self.min_traces
            ):
                stop = "healthy"
            else:
                # 4. stall check on recent source="improve" history
                if imp_store is not None:
                    history = await imp_store.load_history(
                        stem, source="improve", limit=self.stall_window
                    )
                    stall_verdict = detect_stall(
                        history, target_hqs=self.target_hqs,
                        drift_threshold=self.drift_threshold,
                    )
                # 5. escalate to optimize only when stalled and under the cap
                if stall_verdict is not None and stall_verdict.stalled:
                    if escalations < self.max_escalations:
                        opt_result = await self._optimize_factory(
                            self.spec_path, self.trace_db
                        ).optimize()
                        optimize_ran = True
                        escalations += 1
                        if opt_result is not None:
                            optimize_accepted = opt_result.accepted
                            if opt_result.accepted and self.auto_apply:
                                OptimizerRunner.apply_diff(
                                    self.spec_path, opt_result.proposed_diff
                                )
                                optimize_applied = True
                    else:
                        stop = "escalation_cap"

            iterations.append(TuneIteration(
                iteration=it, run_hqs=run_hqs,
                improve_applied=report.applied,
                improve_escalated_oscillation=report.escalated_oscillation,
                stall=stall_verdict, optimize_ran=optimize_ran,
                optimize_accepted=optimize_accepted, optimize_applied=optimize_applied,
                llm_calls=calls, tokens=toks, wall_s=accumulated["wall_s"],
                stop_reason=stop,
            ))

            if stop is not None:
                stop_reason = stop
                break

            # 6. budget stops
            if self.max_llm_calls is not None and accumulated["llm_calls"] >= self.max_llm_calls:
                stop_reason = "budget_llm_calls"
                break
            if self.max_tokens is not None and accumulated["tokens"] >= self.max_tokens:
                stop_reason = "budget_tokens"
                break
            if self.max_wallclock is not None and accumulated["wall_s"] >= self.max_wallclock:
                stop_reason = "budget_wallclock"
                break

        return TuneResult(
            iterations=iterations, stop_reason=stop_reason, final_hqs=final_hqs,
            escalations=escalations, llm_calls=accumulated["llm_calls"],
            tokens=accumulated["tokens"], wall_s=accumulated["wall_s"], error=error,
        )