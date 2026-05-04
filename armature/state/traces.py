from __future__ import annotations
import json
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field


class TraceRecord(BaseModel):
    run_id: str
    workflow_name: str
    stage_id: str
    role_type: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    output_valid: bool = True
    quorum_score: float | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


class IhrResult(BaseModel):
    run_id: str
    ihr: float
    output_valid_rate: float
    success_rate: float
    avg_quorum_score: float
    latency_score: float
    n_traces: int


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS traces (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT NOT NULL,
        workflow_name TEXT NOT NULL,
        stage_id    TEXT NOT NULL,
        role_type   TEXT NOT NULL,
        model       TEXT NOT NULL,
        input_tokens  INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        latency_ms  REAL DEFAULT 0.0,
        success     INTEGER NOT NULL DEFAULT 1,
        output_valid INTEGER NOT NULL DEFAULT 1,
        quorum_score REAL,
        timestamp   TEXT NOT NULL,
        inputs_json TEXT DEFAULT '{}',
        outputs_json TEXT DEFAULT '{}'
    )
"""


class TraceStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, trace: TraceRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO traces
                   (run_id, workflow_name, stage_id, role_type, model,
                    input_tokens, output_tokens, latency_ms, success, output_valid,
                    quorum_score, timestamp, inputs_json, outputs_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trace.run_id, trace.workflow_name, trace.stage_id,
                    trace.role_type, trace.model,
                    trace.input_tokens, trace.output_tokens, trace.latency_ms,
                    int(trace.success), int(trace.output_valid),
                    trace.quorum_score, trace.timestamp,
                    json.dumps(trace.inputs), json.dumps(trace.outputs),
                ),
            )
            await db.commit()

    async def query(
        self,
        workflow_name: str | None = None,
        min_quorum_score: float | None = None,
        limit: int = 100,
    ) -> list[TraceRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if workflow_name:
            conditions.append("workflow_name = ?")
            params.append(workflow_name)
        if min_quorum_score is not None:
            conditions.append("quorum_score >= ?")
            params.append(min_quorum_score)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM traces {where} ORDER BY timestamp DESC LIMIT ?", params
            )
            rows = await cursor.fetchall()
        return [self._row_to_trace(r) for r in rows]

    async def high_quality_traces(
        self, workflow_name: str, min_score: float = 0.85
    ) -> list[TraceRecord]:
        return await self.query(workflow_name=workflow_name, min_quorum_score=min_score)

    @staticmethod
    def _row_to_trace(r: "aiosqlite.Row") -> TraceRecord:
        return TraceRecord(
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

    async def query_by_run(self, run_id: str) -> list[TraceRecord]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM traces WHERE run_id = ? ORDER BY timestamp ASC", (run_id,)
            )
            rows = await cursor.fetchall()
        return [self._row_to_trace(r) for r in rows]

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
