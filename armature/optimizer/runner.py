from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Callable
from pydantic import BaseModel
from armature.state.traces import TraceStore


class OptimizationResult(BaseModel):
    accepted: bool
    proposed_diff: str
    rationale: str
    confidence: float
    score: float
    feedback: str


class ABTestResult(BaseModel):
    original_ihr: float
    proposed_ihr: float
    delta: float
    winner: str  # "original" | "proposed" | "tie"
    n_runs: int
    n_inputs: int


class OptimizerRunner:
    MIN_TRACES = 5

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
        traces_json = json.dumps(
            [t.model_dump() if hasattr(t, "model_dump") else {} for t in traces],
            default=str,
        )

        workflow_result = await self._run_optimizer_workflow({
            "traces_json": traces_json,
            "spec_yaml": spec_yaml,
        })

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

    async def _load_traces(self):
        if not self._trace_db_path.exists():
            return []
        store = TraceStore(self._trace_db_path)
        await store.init()
        workflow_name = self._target_spec_path.stem
        return await store.query(workflow_name=workflow_name, limit=20)

    async def _run_optimizer_workflow(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from armature.runtime.engine import Harness
        from armature.spec.loader import load_spec
        spec = load_spec(self._optimizer_spec_path)
        harness = Harness(spec=spec)
        return await harness.run(inputs)

    async def _run_one_and_score(
        self, spec_path: Path, inputs: dict[str, Any]
    ) -> "IhrResult | None":
        from armature.runtime.engine import Harness
        from armature.spec.loader import load_spec
        from armature.state.traces import TraceStore, IhrResult

        spec = load_spec(spec_path)
        harness = Harness(spec=spec)
        await harness.run(inputs)
        store = TraceStore(harness._traces._path)
        return await store.compute_ihr(harness._run_id)

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
