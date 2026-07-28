"""Unified improvement-history store shared by ``armature improve`` and ``armature optimize``.

Both engines write ``ImprovementRecord``s here and read each other's history through
the same store (filtered by ``source``), so the cross-engine handoff uses one record
type, one physical store, and one key (the spec file stem) — instead of improve's
JSONL log plus optimize's ``ProposalStore`` with hand-rolled cross-summaries.

improve's JSONL log (``<spec>.improve_log.jsonl``) remains the local per-cycle audit
read by the dashboard and improve's own closed-loop verification; this store is the
cross-engine substrate.
"""
from __future__ import annotations
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field


Source = Literal["improve", "optimize"]


class ImprovementRecord(BaseModel):
    """A single improvement-cycle entry from either engine.

    A union of both engines' fields, tagged with ``source``. Each engine populates
    the fields it produces and leaves the other engine's fields at their defaults.
    """

    record_id: str
    workflow_stem: str  # the spec file stem — shared cross-engine key
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: Source
    # optimize-populated (improve leaves these default/empty):
    proposed_diff: str = ""
    rationale: str = ""
    confidence: float = 0.0
    accepted: bool = False  # optimize: judge accept; improve: applied
    score: float = 0.0  # optimize: judge score; improve: hqs_before
    feedback: str = ""
    # improve-populated (optimize leaves these empty):
    predicted_fixes: list[str] = Field(default_factory=list)
    predicted_regressions: list[str] = Field(default_factory=list)
    verified_fixes: list[str] = Field(default_factory=list)
    missed_predictions: list[str] = Field(default_factory=list)
    unexpected_regressions: list[str] = Field(default_factory=list)
    applied: bool = False
    hqs_before: float | None = None
    drift_score: float = 0.0
    triggered_by_drift: bool = False
    escalated_oscillation: bool = False
    latency_risk: float = 0.0


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS improvements (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id              TEXT NOT NULL,
        workflow_stem          TEXT NOT NULL,
        timestamp              TEXT NOT NULL,
        source                 TEXT NOT NULL,
        proposed_diff          TEXT NOT NULL,
        rationale              TEXT NOT NULL,
        confidence             REAL NOT NULL,
        accepted               INTEGER NOT NULL,
        score                  REAL NOT NULL,
        feedback               TEXT NOT NULL,
        predicted_fixes        TEXT NOT NULL,
        predicted_regressions  TEXT NOT NULL,
        verified_fixes         TEXT NOT NULL,
        missed_predictions     TEXT NOT NULL,
        unexpected_regressions TEXT NOT NULL,
        applied                INTEGER NOT NULL,
        hqs_before             REAL,
        drift_score            REAL NOT NULL,
        triggered_by_drift     INTEGER NOT NULL,
        escalated_oscillation  INTEGER NOT NULL,
        latency_risk           REAL NOT NULL
    )
"""


def _join(items: list[str]) -> str:
    return "\x1f".join(items)


def _split(text: str) -> list[str]:
    return [s for s in text.split("\x1f") if s]


class ImprovementStore:
    """SQLite-backed history of ``ImprovementRecord``s, keyed by workflow stem."""

    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, rec: ImprovementRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO improvements
                   (record_id, workflow_stem, timestamp, source, proposed_diff,
                    rationale, confidence, accepted, score, feedback,
                    predicted_fixes, predicted_regressions, verified_fixes,
                    missed_predictions, unexpected_regressions, applied,
                    hqs_before, drift_score, triggered_by_drift,
                    escalated_oscillation, latency_risk)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.record_id, rec.workflow_stem, rec.timestamp, rec.source,
                    rec.proposed_diff, rec.rationale, rec.confidence,
                    int(rec.accepted), rec.score, rec.feedback,
                    _join(rec.predicted_fixes), _join(rec.predicted_regressions),
                    _join(rec.verified_fixes), _join(rec.missed_predictions),
                    _join(rec.unexpected_regressions), int(rec.applied),
                    rec.hqs_before, rec.drift_score, int(rec.triggered_by_drift),
                    int(rec.escalated_oscillation), rec.latency_risk,
                ),
            )
            await db.commit()

    async def load_history(
        self, workflow_stem: str, source: str | None = None, limit: int = 20
    ) -> list[ImprovementRecord]:
        """Return records for ``workflow_stem``, newest-first.

        If ``source`` is given (``"improve"`` or ``"optimize"``), filter to that
        engine's records — this is how each engine reads the other's history.
        """
        query = "SELECT * FROM improvements WHERE workflow_stem = ?"
        params: list = [workflow_stem]
        if source is not None:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(r) -> ImprovementRecord:
        return ImprovementRecord(
            record_id=r["record_id"],
            workflow_stem=r["workflow_stem"],
            timestamp=r["timestamp"],
            source=r["source"],
            proposed_diff=r["proposed_diff"],
            rationale=r["rationale"],
            confidence=r["confidence"],
            accepted=bool(r["accepted"]),
            score=r["score"],
            feedback=r["feedback"],
            predicted_fixes=_split(r["predicted_fixes"]),
            predicted_regressions=_split(r["predicted_regressions"]),
            verified_fixes=_split(r["verified_fixes"]),
            missed_predictions=_split(r["missed_predictions"]),
            unexpected_regressions=_split(r["unexpected_regressions"]),
            applied=bool(r["applied"]),
            hqs_before=r["hqs_before"],
            drift_score=r["drift_score"],
            triggered_by_drift=bool(r["triggered_by_drift"]),
            escalated_oscillation=bool(r["escalated_oscillation"]),
            latency_risk=r["latency_risk"],
        )