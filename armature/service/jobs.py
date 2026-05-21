from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Job:
    job_id: str
    status: JobStatus = JobStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    run_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    events: asyncio.Queue = field(default_factory=asyncio.Queue)


class JobStore:
    TTL_SECONDS = 3600

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self) -> Job:
        job = Job(job_id=str(uuid.uuid4()))
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def mark_running(self, job_id: str) -> None:
        self._jobs[job_id].status = JobStatus.RUNNING

    async def mark_complete(self, job_id: str, run_id: str, result: dict[str, Any]) -> None:
        job = self._jobs[job_id]
        job.status = JobStatus.COMPLETE
        job.run_id = run_id
        job.result = result
        await job.events.put({"type": "run_complete", "run_id": run_id})

    async def mark_failed(self, job_id: str, error: str) -> None:
        job = self._jobs[job_id]
        job.status = JobStatus.FAILED
        job.error = error
        await job.events.put({"type": "run_complete", "error": error})

    async def emit_stage_event(self, job_id: str, event: dict[str, Any]) -> None:
        job = self._jobs.get(job_id)
        if job:
            await job.events.put(event)
