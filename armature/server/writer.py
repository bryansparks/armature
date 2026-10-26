"""The single writer for run events and the run, step, cost, and finding rows they imply.

One thread owns one connection and flushes every second or every 500 events,
in one transaction for all runs together. Callers on the event loop only
append to memory, so a slow database never stalls broadcasting. When a flush
fails while the database still answers, the batch is written again in halves
until only the rows that fail are left out, so one bad row never drops others.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select, text, update

from armature.events.summary import merge_summary
from armature.server import runners
from armature.server.db import new_id
from armature.server.db.schema import (
    findings,
    run_artifacts,
    run_events,
    run_steps,
    runs,
)

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 1.0
FLUSH_EVENTS = 500
MAX_BACKLOG = 100_000
MAX_ATTEMPTS = 5
STEP_KEY_MAX = 191


def step_key(step_id: str, parent_step: str | None) -> str:
    """The run_steps key: a child's step is filed under its parent, so a reused stage id never merges."""
    key = f"{parent_step}/{step_id}" if parent_step else step_id
    if len(key) <= STEP_KEY_MAX:
        return key
    return key[:STEP_KEY_MAX - 41] + "#" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


@dataclass
class StepChange:
    step_id: str = ""
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    parent_step: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    output: Any = None
    has_output: bool = False


@dataclass
class RunChange:
    last_event_at: datetime | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    end: tuple[str, datetime, str | None] | None = None
    steps: dict[str, StepChange] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    summary: dict[str, Any] | None = None


@dataclass
class Batch:
    events: list[dict[str, Any]] = field(default_factory=list)
    by_run: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    runs: dict[str, RunChange] = field(default_factory=dict)
    attempts: int = 0

    def add(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.by_run.setdefault(event["run_id"], []).append(event)

    def run(self, run_id: str) -> RunChange:
        change = self.runs.get(run_id)
        if change is None:
            change = self.runs[run_id] = RunChange()
        return change


class EventWriter:
    def __init__(self, engine: Engine, clock: Callable[[], datetime], *, interval_s: float = FLUSH_INTERVAL_S,
                 flush_events: int = FLUSH_EVENTS):
        self.engine = engine
        self.clock = clock
        self.interval_s = interval_s
        self.flush_events = flush_events
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="armature-writer")
        self._conn: Connection | None = None
        self._batch = Batch()
        self._inflight: Batch | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.flushes = 0
        self.rows = 0
        self.failures = 0
        self.dropped = 0
        self.dropped_changes = 0
        self.flush_ms: list[float] = []
        self.flush_rows: list[int] = []

    # Everything below runs on the event loop.

    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="armature-writer")

    async def stop(self) -> None:
        try:
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
        finally:
            await self.flush()
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
            self._executor.shutdown(wait=True)

    def add_event(self, run_id: str, event: dict[str, Any]) -> RunChange:
        """Queue one event row. The returned change collects what the event does to its run."""
        self._batch.add({"run_id": run_id, **event})
        if len(self._batch.events) >= self.flush_events and self._wake is not None:
            self._wake.set()
        return self._batch.run(run_id)

    def add_artifact(self, run_id: str, artifact: dict[str, Any]) -> None:
        """Queue an artifact row, replacing any queued one of the same name."""
        self._batch.run(run_id).artifacts[artifact["name"]] = artifact

    def pending_artifacts(self, run_id: str) -> dict[str, dict[str, Any]]:
        """This run's artifacts not stored yet, by name, the newest of each."""
        pending: dict[str, dict[str, Any]] = {}
        for batch in (self._inflight, self._batch):
            change = batch.runs.get(run_id) if batch is not None else None
            if change is not None:
                pending.update(change.artifacts)
        return pending

    def _run_lists(self, run_id: str) -> list[list[dict[str, Any]]]:
        lists = [self._inflight.by_run.get(run_id)] if self._inflight is not None else []
        return [rows for rows in (*lists, self._batch.by_run.get(run_id)) if rows]

    def pending(self, run_id: str, after: int) -> list[dict[str, Any]]:
        """This run's events not stored yet, oldest first. Costs only this run's own backlog."""
        return [e for rows in self._run_lists(run_id) for e in rows if e["seq"] > after]

    def max_seq(self, run_id: str) -> int:
        lists = self._run_lists(run_id)
        return lists[-1][-1]["seq"] if lists else 0

    @property
    def backlog(self) -> int:
        return len(self._batch.events) + (len(self._inflight.events) if self._inflight is not None else 0)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), self.interval_s)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.flush()

    async def flush(self) -> None:
        batch, self._batch = self._batch, Batch()
        if not batch.events and not batch.runs:
            return
        self._inflight = batch
        started = time.perf_counter()
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._write, batch)
        except Exception:
            self.failures += 1
            log.exception("run event flush failed, keeping %d events for the next one", len(batch.events))
            self._requeue(batch)
            return
        finally:
            self._inflight = None
        self.flushes += 1
        self.rows += len(batch.events)
        self.flush_ms.append((time.perf_counter() - started) * 1000)
        self.flush_rows.append(len(batch.events))
        del self.flush_ms[:-2000], self.flush_rows[:-2000]

    def _requeue(self, failed: Batch) -> None:
        failed.attempts += 1
        if failed.attempts >= MAX_ATTEMPTS or len(failed.events) + len(self._batch.events) > MAX_BACKLOG:
            self.dropped += len(failed.events)
            log.error("dropping %d run events after %d failed flushes", len(failed.events), failed.attempts)
            return
        newer, self._batch = self._batch, failed
        for event in newer.events:
            self._batch.add(event)
        for run_id, change in newer.runs.items():
            _merge_run(self._batch.run(run_id), change)

    async def run_in_writer(self, fn: Callable[[Connection], Any]) -> Any:
        """Run ``fn`` in a transaction on the writer's connection, after any flush in progress."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._transact, fn)

    # Everything below runs on the writer thread.

    def _connection(self) -> Connection:
        if self._conn is None:
            self._conn = self.engine.connect()
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _transact(self, fn: Callable[[Connection], Any]) -> Any:
        conn = self._connection()
        try:
            with conn.begin():
                return fn(conn)
        except Exception:
            self._close()
            raise

    def _reachable(self) -> bool:
        try:
            self._transact(lambda conn: conn.execute(text("SELECT 1")))
            return True
        except Exception:  # noqa: BLE001
            return False

    def _write(self, batch: Batch) -> None:
        try:
            self._transact(lambda conn: _apply(conn, batch))
            return
        except Exception:
            if not self._reachable():
                raise
            log.exception("run event flush failed with the database up, writing it in parts")
        self._insert_bisect(batch.events)
        for run_id, change in batch.runs.items():
            try:
                self._transact(lambda conn, r=run_id, c=change: _apply_run(conn, r, c))
            except Exception:
                self.dropped_changes += 1
                log.exception("dropping the run, step, and finding changes of run %s", run_id)

    def _insert_bisect(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            self._transact(lambda conn: _insert_events(conn, events))
        except Exception:
            if len(events) == 1:
                self.dropped += 1
                log.exception("dropping run event %s seq %s", events[0]["run_id"], events[0]["seq"])
                return
            middle = len(events) // 2
            self._insert_bisect(events[:middle])
            self._insert_bisect(events[middle:])


def _merge_run(into: RunChange, change: RunChange) -> None:
    if change.last_event_at is not None:
        into.last_event_at = max(filter(None, (into.last_event_at, change.last_event_at)))
    into.cost_usd += change.cost_usd
    into.input_tokens += change.input_tokens
    into.output_tokens += change.output_tokens
    into.end = change.end or into.end
    for key, step in change.steps.items():
        merge_step(into.steps.setdefault(key, StepChange(step_id=step.step_id)), step)
    into.findings.extend(change.findings)
    into.artifacts.update(change.artifacts)
    if change.summary:
        into.summary = merge_summary(into.summary, change.summary)


def merge_step(into: StepChange, step: StepChange) -> None:
    into.status = step.status or into.status
    into.started_at = step.started_at or into.started_at
    into.finished_at = step.finished_at or into.finished_at
    into.error = step.error or into.error
    into.parent_step = step.parent_step or into.parent_step
    into.cost_usd += step.cost_usd
    into.input_tokens += step.input_tokens
    into.output_tokens += step.output_tokens
    if step.has_output:
        into.output, into.has_output = step.output, True


def _money(value: float) -> Decimal:
    return Decimal(repr(value)).quantize(Decimal("0.000001"))


def _insert_events(conn: Connection, events: list[dict[str, Any]]) -> None:
    conn.execute(insert(run_events), [
        {"run_id": e["run_id"], "seq": e["seq"], "ts": e["ts"], "kind": e["kind"], "step_id": e["step_id"],
         "payload": e["payload"]} for e in events])


def _apply(conn: Connection, batch: Batch) -> None:
    if batch.events:
        _insert_events(conn, batch.events)
    for run_id, change in batch.runs.items():
        _apply_run(conn, run_id, change)


def _apply_run(conn: Connection, run_id: str, change: RunChange) -> None:
    values: dict[str, Any] = {}
    if change.cost_usd:
        values["cost_usd"] = func.coalesce(runs.c.cost_usd, 0) + _money(change.cost_usd)
    if change.input_tokens:
        values["input_tokens"] = func.coalesce(runs.c.input_tokens, 0) + change.input_tokens
    if change.output_tokens:
        values["output_tokens"] = func.coalesce(runs.c.output_tokens, 0) + change.output_tokens
    if change.last_event_at is not None:
        values["last_event_at"] = change.last_event_at
    if change.end is not None:
        status, finished_at, error = change.end
        values.update(status=status, finished_at=finished_at, error=error)
    if change.summary:
        current = conn.execute(select(runs.c.summary).where(runs.c.id == run_id)).scalar()
        values["summary"] = merge_summary(current, change.summary)
    if values:
        conn.execute(update(runs).where(runs.c.id == run_id).values(**values))
    if change.end is None and change.last_event_at is not None:
        # An event for a run the sweep marked lost means it is still going.
        conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "lost")
                     .values(status="running", finished_at=None))
    for key, step in change.steps.items():
        _upsert_step(conn, run_id, key, step)
    if change.findings:
        conn.execute(insert(findings), [{"id": new_id(), "run_id": run_id, **f} for f in change.findings])
    for name, artifact in change.artifacts.items():
        conn.execute(delete(run_artifacts).where(run_artifacts.c.run_id == run_id, run_artifacts.c.name == name))
        conn.execute(insert(run_artifacts).values(run_id=run_id, **artifact))
    if change.end is not None:
        runners.revoke_tokens(conn, [run_id])


def _upsert_step(conn: Connection, run_id: str, key: str, step: StepChange) -> None:
    where = (run_steps.c.run_id == run_id, run_steps.c.step_key == key)
    if conn.execute(select(run_steps.c.status).where(*where)).first() is None:
        conn.execute(insert(run_steps).values(
            run_id=run_id, step_key=key, step_id=step.step_id, status=step.status or "running",
            started_at=step.started_at, finished_at=step.finished_at, error=step.error,
            parent_step=step.parent_step, cost_usd=_money(step.cost_usd), input_tokens=step.input_tokens,
            output_tokens=step.output_tokens, **({"output": step.output} if step.has_output else {})))
        return
    values: dict[str, Any] = {}
    for name in ("status", "started_at", "finished_at", "error", "parent_step"):
        if getattr(step, name) is not None:
            values[name] = getattr(step, name)
    if step.cost_usd:
        values["cost_usd"] = func.coalesce(run_steps.c.cost_usd, 0) + _money(step.cost_usd)
    if step.input_tokens:
        values["input_tokens"] = func.coalesce(run_steps.c.input_tokens, 0) + step.input_tokens
    if step.output_tokens:
        values["output_tokens"] = func.coalesce(run_steps.c.output_tokens, 0) + step.output_tokens
    if step.has_output:
        values["output"] = step.output
    if values:
        conn.execute(update(run_steps).where(*where).values(**values))


def sweep_lost(conn: Connection, now: datetime, after: timedelta) -> list[str]:
    """Mark running runs with no event for ``after`` as lost. Returns only the runs this call changed.

    Each update repeats the stale check, so a run that heard an event or ended since the read keeps its status.
    """
    last = func.coalesce(runs.c.last_event_at, runs.c.started_at)
    stale = (runs.c.status == "running", last < now - after)
    lost = []
    for run_id in conn.execute(select(runs.c.id).where(*stale)).scalars().all():
        changed = conn.execute(update(runs).where(runs.c.id == run_id, *stale).values(status="lost", finished_at=last))
        if changed.rowcount == 1:
            lost.append(run_id)
    return lost
