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
