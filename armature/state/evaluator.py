"""EvaluationRunner — scores stage outputs against declarative criteria using an LLM."""
from __future__ import annotations
import json
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
import litellm
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from armature.spec.models import HarnessSpec
    from armature.state.traces import TraceStore


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


class EvaluationResult(BaseModel):
    run_id: str
    workflow_name: str
    stage_id: str
    score: float                        # 0.0 – 1.0
    criteria_passed: list[str] = Field(default_factory=list)
    criteria_failed: list[str] = Field(default_factory=list)
    notes: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS evaluations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        TEXT NOT NULL,
        workflow_name TEXT NOT NULL,
        stage_id      TEXT NOT NULL,
        score         REAL NOT NULL,
        criteria_passed TEXT NOT NULL DEFAULT '[]',
        criteria_failed TEXT NOT NULL DEFAULT '[]',
        notes         TEXT NOT NULL DEFAULT '',
        timestamp     TEXT NOT NULL
    )
"""


class EvaluationStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, result: EvaluationResult) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO evaluations "
                "(run_id, workflow_name, stage_id, score, criteria_passed, criteria_failed, notes, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    result.run_id, result.workflow_name, result.stage_id, result.score,
                    json.dumps(result.criteria_passed), json.dumps(result.criteria_failed),
                    result.notes, result.timestamp,
                ),
            )
            await db.commit()

    async def load_for_run(self, run_id: str) -> list[EvaluationResult]:
        if not self._path.exists():
            return []
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM evaluations WHERE run_id=? ORDER BY timestamp ASC", (run_id,)
            )
            rows = await cursor.fetchall()
        return [self._row_to_result(r) for r in rows]

    async def load_for_workflow(self, workflow_name: str) -> list[EvaluationResult]:
        if not self._path.exists():
            return []
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM evaluations WHERE workflow_name=? ORDER BY timestamp DESC",
                (workflow_name,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_result(r) for r in rows]

    @staticmethod
    def _row_to_result(r: "aiosqlite.Row") -> EvaluationResult:
        return EvaluationResult(
            run_id=r["run_id"],
            workflow_name=r["workflow_name"],
            stage_id=r["stage_id"],
            score=r["score"],
            criteria_passed=json.loads(r["criteria_passed"] or "[]"),
            criteria_failed=json.loads(r["criteria_failed"] or "[]"),
            notes=r["notes"] or "",
            timestamp=r["timestamp"],
        )


_SYSTEM_PROMPT = """\
You are a quality evaluator for AI workflow outputs. You will be given the output of a \
workflow stage and a list of criteria to evaluate it against.

For each criterion, determine whether it PASSED or FAILED and provide a brief reason.

Return ONLY a JSON object in this exact format:
{
  "criteria": [
    {"criterion": "...", "passed": true, "reason": "..."}
  ],
  "score": 0.0,
  "notes": "..."
}

score must be a float between 0.0 (all failed) and 1.0 (all passed), proportional to \
the fraction of criteria that passed. notes is a brief overall summary.
"""


class EvaluationRunner:
    """Scores stage outputs against declarative criteria from the spec."""

    def __init__(self, model: str, evaluation_store: EvaluationStore | None = None):
        self._model = model
        self._evaluation_store = evaluation_store

    async def evaluate_run(
        self,
        run_id: str,
        spec: "HarnessSpec",
        trace_store: "TraceStore",
    ) -> list[EvaluationResult]:
        """Score every stage that has evaluate criteria. Returns one result per scored stage."""
        stages_with_criteria = [s for s in spec.stages if s.evaluate]
        if not stages_with_criteria:
            return []

        results: list[EvaluationResult] = []

        for stage in stages_with_criteria:
            traces = await trace_store.query(
                workflow_name=spec.name,
                stage_id=stage.id,
            )
            stage_traces = [t for t in traces if t.run_id == run_id]
            if not stage_traces:
                continue

            trace = stage_traces[0]
            result = await self._score_stage(
                run_id=run_id,
                workflow_name=spec.name,
                stage_id=stage.id,
                outputs=trace.outputs,
                criteria=stage.evaluate,
            )
            results.append(result)

            if self._evaluation_store is not None:
                try:
                    await self._evaluation_store.record(result)
                except Exception:
                    pass  # storage failure must never block evaluation

        return results

    async def _score_stage(
        self,
        run_id: str,
        workflow_name: str,
        stage_id: str,
        outputs: dict[str, Any],
        criteria: list[str],
    ) -> EvaluationResult:
        criteria_lines = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
        outputs_text = json.dumps(outputs, indent=2, default=str)

        try:
            response = await litellm_completion(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Stage output:\n{outputs_text}\n\n"
                            f"Criteria to evaluate:\n{criteria_lines}"
                        ),
                    },
                ],
            )
            content = response.choices[0].message.content or ""
            data = json.loads(content)

            passed = [
                item["criterion"]
                for item in data.get("criteria", [])
                if item.get("passed")
            ]
            failed = [
                item["criterion"]
                for item in data.get("criteria", [])
                if not item.get("passed")
            ]
            score = float(data.get("score", len(passed) / max(len(criteria), 1)))
            notes = str(data.get("notes", ""))

        except Exception:
            passed, failed = [], list(criteria)
            score = 0.0
            notes = "Evaluation failed — LLM error or malformed response."

        return EvaluationResult(
            run_id=run_id,
            workflow_name=workflow_name,
            stage_id=stage_id,
            score=score,
            criteria_passed=passed,
            criteria_failed=failed,
            notes=notes,
        )
