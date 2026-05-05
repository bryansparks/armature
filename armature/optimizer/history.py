from __future__ import annotations
import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field


class ProposalRecord(BaseModel):
    proposal_id: str
    workflow_name: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    proposed_diff: str
    rationale: str
    confidence: float
    accepted: bool
    score: float
    feedback: str


_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS proposals (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id   TEXT NOT NULL,
        workflow_name TEXT NOT NULL,
        timestamp     TEXT NOT NULL,
        proposed_diff TEXT NOT NULL,
        rationale     TEXT NOT NULL,
        confidence    REAL NOT NULL,
        accepted      INTEGER NOT NULL,
        score         REAL NOT NULL,
        feedback      TEXT NOT NULL
    )
"""


class ProposalStore:
    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(_CREATE_SQL)
            await db.commit()

    async def record(self, proposal: ProposalRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO proposals
                   (proposal_id, workflow_name, timestamp, proposed_diff,
                    rationale, confidence, accepted, score, feedback)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    proposal.proposal_id, proposal.workflow_name, proposal.timestamp,
                    proposal.proposed_diff, proposal.rationale, proposal.confidence,
                    int(proposal.accepted), proposal.score, proposal.feedback,
                ),
            )
            await db.commit()

    async def load_history(
        self, workflow_name: str, limit: int = 20
    ) -> list[ProposalRecord]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM proposals WHERE workflow_name = ?
                   ORDER BY timestamp DESC, id DESC LIMIT ?""",
                (workflow_name, limit),
            )
            rows = await cursor.fetchall()
        return [
            ProposalRecord(
                proposal_id=r["proposal_id"],
                workflow_name=r["workflow_name"],
                timestamp=r["timestamp"],
                proposed_diff=r["proposed_diff"],
                rationale=r["rationale"],
                confidence=r["confidence"],
                accepted=bool(r["accepted"]),
                score=r["score"],
                feedback=r["feedback"],
            )
            for r in rows
        ]
