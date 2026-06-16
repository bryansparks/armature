from __future__ import annotations
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal
from pydantic import BaseModel
from armature.state.traces import TraceStore

if TYPE_CHECKING:
    from armature.state.traces import HqsResult


class OptimizationResult(BaseModel):
    accepted: bool
    proposed_diff: str
    rationale: str
    confidence: float
    score: float
    feedback: str


class ABTestResult(BaseModel):
    original_hqs: float
    proposed_hqs: float
    delta: float
    winner: Literal["original", "proposed", "tie"]
    n_runs: int
    n_inputs: int


class LoopResult(BaseModel):
    iterations: list[OptimizationResult]
    accepted_count: int
    rejected_count: int


class OptimizerRunner:
    MIN_TRACES = 5

    def __init__(
        self,
        target_spec_path: Path | str,
        trace_db_path: Path | str,
        optimizer_spec_path: Path | str | None = None,
        metric_fn: Callable[[dict[str, Any]], float] | None = None,
        proposal_db_path: Path | str | None = None,
        model_override: str | None = None,
    ):
        self._target_spec_path = Path(target_spec_path)
        self._trace_db_path = Path(trace_db_path)
        self._optimizer_spec_path = optimizer_spec_path or (
            Path(__file__).parent / "workflow.yaml"
        )
        self._metric_fn = metric_fn
        self._proposal_db_path = Path(proposal_db_path) if proposal_db_path else None
        self._model_override = model_override

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

        proposal_store = None
        if self._proposal_db_path is not None:
            from armature.optimizer.history import ProposalStore
            proposal_store = ProposalStore(self._proposal_db_path)
            await proposal_store.init()
            try:
                history = await proposal_store.load_history(self._target_spec_path.stem)
                if history:
                    workflow_inputs["proposal_history_json"] = json.dumps(
                        [p.model_dump() for p in history], default=str
                    )
            except Exception:
                pass  # history is advisory — never block optimization on DB errors

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

        if proposal_store is not None:
            from armature.optimizer.history import ProposalRecord
            await proposal_store.record(ProposalRecord(
                proposal_id=str(uuid.uuid4()),
                workflow_name=self._target_spec_path.stem,
                proposed_diff=result.proposed_diff,
                rationale=result.rationale,
                confidence=result.confidence,
                accepted=result.accepted,
                score=result.score,
                feedback=result.feedback,
            ))

        return result

    async def _load_traces(self):
        if not self._trace_db_path.exists():
            return []
        store = TraceStore(self._trace_db_path)
        await store.init()
        workflow_name = self._target_spec_path.stem
        return await store.query(workflow_name=workflow_name, limit=20)

    async def _run_optimizer_workflow(self, inputs: dict[str, Any]) -> dict[str, Any]:
        import os
        from armature.runtime.engine import Harness
        from armature.spec.loader import load_spec
        from armature.spec.models import ModelTierConfig
        spec = load_spec(self._optimizer_spec_path)
        # Allow callers or ARMATURE_REFINER_MODEL env var to override the optimizer model
        model_str = self._model_override or os.environ.get("ARMATURE_REFINER_MODEL")
        if model_str:
            parts = model_str.split("/", 1)
            provider, model = (parts[0], parts[1]) if len(parts) == 2 else ("anthropic", model_str)
            spec.model_tiers.frontier = ModelTierConfig(provider=provider, model=model)
        harness = Harness(spec=spec)
        return await harness.run(inputs)

    async def _run_one_and_score(
        self, spec_path: Path, inputs: dict[str, Any]
    ) -> "HqsResult | None":
        from armature.runtime.engine import Harness
        from armature.spec.loader import load_spec

        spec = load_spec(spec_path)
        with tempfile.TemporaryDirectory() as tmp_dir:
            harness = Harness(spec=spec, session_dir=Path(tmp_dir))
            await harness.run(inputs)
            store = TraceStore(harness._traces._path)
            return await store.compute_hqs(harness._run_id)

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
                    hqs = await self._run_one_and_score(spec_path, inp)
                    if hqs is not None:
                        scores.append(hqs.hqs)
            return scores

        original_scores = await score_spec(self._target_spec_path)
        proposed_scores = await score_spec(proposed_spec_path)

        original_hqs = sum(original_scores) / len(original_scores) if original_scores else 0.0
        proposed_hqs = sum(proposed_scores) / len(proposed_scores) if proposed_scores else 0.0
        delta = proposed_hqs - original_hqs

        if delta > 0.01:
            winner = "proposed"
        elif delta < -0.01:
            winner = "original"
        else:
            winner = "tie"

        return ABTestResult(
            original_hqs=original_hqs,
            proposed_hqs=proposed_hqs,
            delta=delta,
            winner=winner,
            n_runs=n_runs,
            n_inputs=len(inputs_sample),
        )

    async def run_loop(
        self, n_iterations: int = 5, auto_apply: bool = False
    ) -> LoopResult:
        iterations: list[OptimizationResult] = []
        for _ in range(n_iterations):
            result = await self.optimize()
            if result is None:
                break   # not enough traces — no point continuing
            if result.accepted and auto_apply:
                ok, msg = self.apply_diff(self._target_spec_path, result.proposed_diff)
                result = result.model_copy(update={"feedback": f"{result.feedback} | apply: {msg}"})
            iterations.append(result)

        accepted = sum(1 for r in iterations if r.accepted)
        return LoopResult(
            iterations=iterations,
            accepted_count=accepted,
            rejected_count=len(iterations) - accepted,
        )

    @staticmethod
    def apply_diff(spec_path: Path, diff_text: str) -> tuple[bool, str]:
        """Apply a unified diff to spec_path using the system patch utility.

        Creates a .orig backup of the original file before patching.
        Returns (success, message).
        """
        if not shutil.which("patch"):
            return False, "'patch' command not found — install GNU patch and retry"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as f:
            f.write(diff_text)
            patch_file = Path(f.name)

        try:
            proc = subprocess.run(
                ["patch", "--backup", str(spec_path), str(patch_file)],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                return True, f"Patched {spec_path.name} (backup at {spec_path.name}.orig)"
            return False, (proc.stderr or proc.stdout).strip()
        except Exception as exc:
            return False, str(exc)
        finally:
            patch_file.unlink(missing_ok=True)
