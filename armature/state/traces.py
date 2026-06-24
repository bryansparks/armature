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
    error_type: str | None = None
    escalation_count: int = 0
    spec_version: str = ""
    inputs_hash: str = ""
    policy_version: str = ""
    inputs_provenance: dict[str, str] = Field(default_factory=dict)
    tools_declared: list[str] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    sandbox_image_digest: str | None = None
    loop_iteration: int | None = None  # set for stages running inside a loop (1-based)
    agent_id: str | None = None
    agent_version: str | None = None
    active_skill_ids: list[str] = Field(default_factory=list)


class HqsResult(BaseModel):
    run_id: str
    hqs: float
    output_valid_rate: float
    success_rate: float
    avg_quorum_score: float
    latency_score: float
    n_traces: int
    avg_escalation_count: float = 0.0
    hfr: float = 0.0


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
        outputs_json TEXT DEFAULT '{}',
        error_type  TEXT,
        escalation_count INTEGER DEFAULT 0,
        spec_version TEXT DEFAULT ''
    )
"""


class TraceStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(_CREATE_SQL)
            for col_def in [
                "error_type TEXT",
                "escalation_count INTEGER DEFAULT 0",
                "spec_version TEXT DEFAULT ''",
                "inputs_hash TEXT DEFAULT ''",
                "policy_version TEXT DEFAULT ''",
                "inputs_provenance_json TEXT DEFAULT '{}'",
                "tools_declared_json TEXT DEFAULT '[]'",
                "tools_called_json TEXT DEFAULT '[]'",
                "sandbox_image_digest TEXT",
                "loop_iteration INTEGER",
                "agent_id TEXT",
                "agent_version TEXT",
                "active_skill_ids_json TEXT DEFAULT '[]'",
            ]:
                col = col_def.split()[0]
                try:
                    await db.execute(f"ALTER TABLE traces ADD COLUMN {col_def}")
                except Exception:
                    pass  # column already exists
            await db.commit()

    async def record(self, trace: TraceRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO traces
                   (run_id, workflow_name, stage_id, role_type, model,
                    input_tokens, output_tokens, latency_ms, success, output_valid,
                    quorum_score, timestamp, inputs_json, outputs_json,
                    error_type, escalation_count, spec_version,
                    inputs_hash, policy_version, inputs_provenance_json,
                    tools_declared_json, tools_called_json,
                    sandbox_image_digest, loop_iteration,
                    agent_id, agent_version, active_skill_ids_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trace.run_id, trace.workflow_name, trace.stage_id,
                    trace.role_type, trace.model,
                    trace.input_tokens, trace.output_tokens, trace.latency_ms,
                    int(trace.success), int(trace.output_valid),
                    trace.quorum_score, trace.timestamp,
                    json.dumps(trace.inputs), json.dumps(trace.outputs),
                    trace.error_type, trace.escalation_count, trace.spec_version,
                    trace.inputs_hash, trace.policy_version,
                    json.dumps(trace.inputs_provenance),
                    json.dumps(trace.tools_declared),
                    json.dumps(trace.tools_called),
                    trace.sandbox_image_digest,
                    trace.loop_iteration,
                    trace.agent_id,
                    trace.agent_version,
                    json.dumps(trace.active_skill_ids),
                ),
            )
            await db.commit()

    async def query(
        self,
        workflow_name: str | None = None,
        stage_id: str | None = None,
        min_quorum_score: float | None = None,
        limit: int = 100,
    ) -> list[TraceRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if workflow_name:
            conditions.append("workflow_name = ?")
            params.append(workflow_name)
        if stage_id is not None:
            conditions.append("stage_id = ?")
            params.append(stage_id)
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
        d = dict(r)
        return TraceRecord(
            run_id=d["run_id"],
            workflow_name=d["workflow_name"],
            stage_id=d["stage_id"],
            role_type=d["role_type"],
            model=d["model"],
            input_tokens=d.get("input_tokens") or 0,
            output_tokens=d.get("output_tokens") or 0,
            latency_ms=d.get("latency_ms") or 0.0,
            success=bool(d.get("success", 1)),
            output_valid=bool(d.get("output_valid", 1)),
            quorum_score=d.get("quorum_score"),
            timestamp=d["timestamp"],
            inputs=json.loads(d.get("inputs_json") or "{}"),
            outputs=json.loads(d.get("outputs_json") or "{}"),
            error_type=d.get("error_type") or None,
            escalation_count=d.get("escalation_count") or 0,
            spec_version=d.get("spec_version") or "",
            inputs_hash=d.get("inputs_hash") or "",
            policy_version=d.get("policy_version") or "",
            inputs_provenance=json.loads(d.get("inputs_provenance_json") or "{}"),
            tools_declared=json.loads(d.get("tools_declared_json") or "[]"),
            tools_called=json.loads(d.get("tools_called_json") or "[]"),
            sandbox_image_digest=d.get("sandbox_image_digest") or None,
            loop_iteration=d.get("loop_iteration") or None,
            agent_id=d.get("agent_id") or None,
            agent_version=d.get("agent_version") or None,
            active_skill_ids=json.loads(d.get("active_skill_ids_json") or "[]"),
        )

    async def latest_run_id(self, workflow_name: str) -> str | None:
        """Return the run_id of the most recent run for the given workflow."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT run_id FROM traces WHERE workflow_name = ? ORDER BY timestamp DESC LIMIT 1",
                (workflow_name,),
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def get_run_outputs(self, run_id: str) -> dict[str, dict]:
        """Return {stage_id: outputs_dict} for all stages of a run."""
        traces = await self.query_by_run(run_id)
        return {t.stage_id: t.outputs for t in traces}

    async def query_by_run(self, run_id: str) -> list[TraceRecord]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM traces WHERE run_id = ? ORDER BY timestamp ASC", (run_id,)
            )
            rows = await cursor.fetchall()
        return [self._row_to_trace(r) for r in rows]

    async def compute_hqs(self, run_id: str) -> "HqsResult | None":
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
        avg_escalation_count = sum(t.escalation_count for t in traces) / n
        hfr = sum(1 for t in traces if t.escalation_count == 0) / n

        hqs = (
            0.35 * output_valid_rate
            + 0.25 * success_rate
            + 0.20 * avg_quorum_score
            + 0.10 * latency_score
            + 0.10 * hfr
        )
        return HqsResult(
            run_id=run_id,
            hqs=hqs,
            output_valid_rate=output_valid_rate,
            success_rate=success_rate,
            avg_quorum_score=avg_quorum_score,
            latency_score=latency_score,
            n_traces=n,
            avg_escalation_count=avg_escalation_count,
            hfr=hfr,
        )
