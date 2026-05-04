from __future__ import annotations
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field


class SessionEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SessionLog:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, event: SessionEvent) -> None:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
                f.flush()

    async def read_all(self) -> list[SessionEvent]:
        if not self._path.exists():
            return []
        events = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(SessionEvent.model_validate_json(line))
        return events
