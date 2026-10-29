"""One run with its steps and events, and the Server-Sent Event streams of live runs."""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select

from armature.server import display, rbac, reruns, threads
from armature.server.app.deps import Granted, by_run, require, server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import Session, load_session
from armature.server.db.schema import findings, jobs, run_artifacts, run_steps, runs
from armature.server.ingest import TRUNCATED, list_row, load_summary
from armature.server.live import (
    Message,
    Subscription,
    event_message,
    run_key,
    runs_key,
    summary_message,
)

router = APIRouter()

REPLAY_LIMIT = 500
EVENTS_LIMIT_MAX = 1000
HEARTBEAT_S = 15.0
STREAM_LIFETIME_S = 600.0
FINDINGS_LIMIT = 200
COALESCE_S = 0.15
RETRY_MS = 3000
RETRY_JITTER_MS = 2000
RECONNECT = "event: reconnect\ndata: {}\n\n"
SSE_HEADERS = {"X-Accel-Buffering": "no"}


def _live(request: Request):
    return request.app.state.live


def _step(row: Any, show_cost: bool) -> dict[str, Any]:
    return {"step_key": row.step_key, "step_id": row.step_id, "status": row.status, "started_at": row.started_at,
            "finished_at": row.finished_at,
            "cost_usd": float(row.cost_usd) if row.cost_usd is not None and show_cost else None,
            "error": row.error, "parent_step": row.parent_step}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request,
                  granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    def read() -> tuple[dict[str, Any] | None, list[Any], Any, bool, dict[str, Any] | None, dict[str, Any]]:
        with server(request).engine.connect() as conn:
            rerun = {"can_rerun": rbac.at_least(granted.role, "contributor") and reruns.allowed(conn, run_id),
                     "rerun": reruns.latest(conn, run_id)}
            steps = conn.execute(select(run_steps).where(run_steps.c.run_id == run_id)
                                 .order_by(run_steps.c.started_at, run_steps.c.step_key)).all()
            trigger_ref = conn.execute(select(runs.c.trigger_ref).where(runs.c.id == run_id)).scalar()
            job = conn.execute(select(jobs.c.id, jobs.c.status, jobs.c.cancel_requested_at)
                               .where(jobs.c.run_id == run_id).order_by(jobs.c.created_at.desc(), jobs.c.id)).first()
            return (load_summary(conn, run_id), steps, trigger_ref, display.load(conn)["show_cost"],
                    dict(job._mapping) if job else None, rerun)

    stored, steps, trigger_ref, show_cost, job, rerun = await asyncio.to_thread(read)
    hub = _live(request)
    summary = hub.summary(run_id) or stored
    dry_run = isinstance(trigger_ref, dict) and trigger_ref.get("dry_run") is True
    cost = summary["cost_usd"] if show_cost else None
    return {"run": {**summary, "cost_usd": cost, "last_seq": await hub.last_seq(run_id), "dry_run": dry_run,
                    "links": threads.links(summary.get("subject"), summary.get("summary")), **rerun},
            "steps": [_step(r, show_cost) for r in steps], "job": job}


@router.get("/api/runs/{run_id}/events")
async def get_events(run_id: str, request: Request, after: int = Query(0, ge=0),
                     before: int | None = Query(None, ge=1),
                     limit: int = Query(REPLAY_LIMIT, ge=1, le=EVENTS_LIMIT_MAX),
                     granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    """Forward pages from ``after`` (``next_after``), or backward pages below ``before`` (``prev_before``)."""
    hub = _live(request)
    if before is not None:
        events = await hub.events_before(run_id, before, limit)
        first = events[0]["seq"] if events else None
        return {"events": events, "prev_before": first if first is not None and first > 1 else None}
    events = await hub.events_after(run_id, after, limit)
    return {"events": events, "next_after": events[-1]["seq"] if len(events) == limit else None}


def _resume_from(request: Request, after: int | None) -> int:
    header = request.headers.get("last-event-id", "").strip()
    if header.isdigit():
        return int(header)
    return after or 0


def _open(request: Request, user_id: str) -> None:
    if not _live(request).running:
        raise ApiError(503, "Live updates are not running.")
    if not _live(request).open_stream(user_id):
        raise ApiError(429, "Too many live streams open.", {"Retry-After": "30"})


def _preamble() -> str:
    return f"retry: {RETRY_MS + random.randint(0, RETRY_JITTER_MS)}\n\n"


Authorize = Callable[[], Awaitable[bool]]


async def _follow(request: Request, sub: Subscription, last_seq: int, rows: bool,
                  authorize: Authorize) -> AsyncIterator[str]:
    """Live frames from ``sub``, at most one write per COALESCE_S.

    Every HEARTBEAT_S it checks the viewer may still watch, and sends a heartbeat if nothing else went out.
    After STREAM_LIFETIME_S it asks the client to reconnect. ``rows`` sends a run's list row, not its summary.
    """
    hub = _live(request)
    ends_at = time.monotonic() + STREAM_LIFETIME_S
    last_write = check_at = time.monotonic()
    while True:
        now = time.monotonic()
        if sub.overflowed or now >= ends_at:
            yield RECONNECT
            return
        if hub.closing:
            return
        if now >= check_at + HEARTBEAT_S:
            if await request.is_disconnected() or not await authorize():
                return
            check_at = now
            if now >= last_write + HEARTBEAT_S:
                last_write = now
                yield ": ping\n\n"
        wait = max(0.0, min(check_at + HEARTBEAT_S, ends_at) - time.monotonic())
        try:
            first = await asyncio.wait_for(sub.queue.get(), wait)
        except TimeoutError:
            continue
        messages: list[Message] = [first]
        while not sub.queue.empty():
            messages.append(sub.queue.get_nowait())
        frames = []
        for message in messages:
            if message.seq is not None:
                if message.seq <= last_seq:
                    continue
                last_seq = message.seq
            frames.append(message.list_frame if rows and message.list_frame else message.frame)
        if frames:
            yield "".join(frames)
            last_write = time.monotonic()
        await asyncio.sleep(COALESCE_S)


def _stream(request: Request, user_id: str, sub: Subscription, first: str, last_seq: int, authorize: Authorize,
            *, rows: bool = False, follow: bool = True) -> StreamingResponse:
    hub = _live(request)

    async def body() -> AsyncIterator[str]:
        try:
            yield _preamble() + first
            if not follow:
                yield RECONNECT
                return
            async for chunk in _follow(request, sub, last_seq, rows, authorize):
                yield chunk
        finally:
            hub.broker.unsubscribe(sub)
            hub.close_stream(user_id)

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)


def _session_check(request: Request) -> Callable[[Callable[[Any, rbac.Access], bool]], Authorize]:
    """Builds a re-check that reloads the session from its cookie, without extending it, and its access."""
    srv = server(request)
    token = request.cookies.get(srv.cookie_name)

    def build(allowed: Callable[[Any, rbac.Access], bool]) -> Authorize:
        def check() -> bool:
            with srv.engine.begin() as conn:
                session = load_session(conn, srv.keyring, token, srv.now(), touch=False)
                if session is None:
                    return False
                return allowed(conn, rbac.load_access(conn, session.user_id, bool(session.user.is_admin)))

        async def authorize() -> bool:
            try:
                return await asyncio.to_thread(check)
            except Exception:  # noqa: BLE001
                return False

        return authorize

    return build


@router.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request, after: int | None = Query(None, ge=0),
                     granted: Granted = Depends(require("viewer", by_run()))) -> StreamingResponse:
    hub = _live(request)
    _open(request, granted.user_id)
    sub = hub.broker.subscribe([run_key(run_id)])
    start = _resume_from(request, after)
    try:
        replay = await hub.events_after(run_id, start, REPLAY_LIMIT)
        summary = hub.summary(run_id) or await asyncio.to_thread(_summary, request, run_id)
    except BaseException:
        hub.broker.unsubscribe(sub)
        hub.close_stream(granted.user_id)
        raise
    last_seq = replay[-1]["seq"] if replay else start
    frames = [summary_message(summary, list_row(summary)).frame] if summary else []
    frames += [event_message(run_id, granted.target.team_id, granted.target.project_id, e).frame for e in replay]

    def allowed(conn, access: rbac.Access) -> bool:
        try:
            return rbac.at_least(rbac.role_for(access, rbac.run_target(conn, run_id)), "viewer")
        except rbac.NotFound:
            return False

    # A full replay may not be the end, so the client reconnects from its last id instead of skipping a gap.
    return _stream(request, granted.user_id, sub, "".join(frames), last_seq, _session_check(request)(allowed),
                   follow=len(replay) < REPLAY_LIMIT)


def _summary(request: Request, run_id: str) -> dict[str, Any] | None:
    with server(request).engine.connect() as conn:
        return load_summary(conn, run_id)


@router.get("/api/stream/runs")
async def stream_runs(request: Request, team: str | None = None, project: str | None = None,
                      session: Session = Depends(signed_in)) -> StreamingResponse:
    hub = _live(request)

    def access() -> rbac.Access:
        with server(request).engine.connect() as conn:
            return rbac.load_access(conn, session.user_id, bool(session.user.is_admin))

    scope = [rbac.visible_scope(await asyncio.to_thread(access))]

    def accept(message: Message) -> bool:
        return (rbac.in_scope(scope[0], message.team_id, message.project_id)
                and (team is None or message.team_id == team)
                and (project is None or message.project_id == project))

    def allowed(_conn, fresh: rbac.Access) -> bool:
        scope[0] = rbac.visible_scope(fresh)
        return True

    _open(request, session.user_id)
    keys = [runs_key(team)] if team else [runs_key(t) for t in scope[0].visible_team_ids]
    if scope[0].all and not team:
        keys = [runs_key(None)]
    sub = hub.broker.subscribe(keys or [runs_key("-")], accept)
    return _stream(request, session.user_id, sub, "", 0, _session_check(request)(allowed), rows=True)


@router.get("/api/runs/{run_id}/findings")
def list_findings(run_id: str, request: Request, before: str | None = None,
                  limit: int = Query(FINDINGS_LIMIT, ge=1, le=EVENTS_LIMIT_MAX),
                  granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    """Newest first. ``next_before`` is the id to pass as ``before`` for the next page."""
    query = select(findings).where(findings.c.run_id == run_id)
    with server(request).engine.connect() as conn:
        if before:
            cursor = conn.execute(select(findings.c.created_at, findings.c.id)
                                  .where(findings.c.run_id == run_id, findings.c.id == before)).first()
            if cursor is None:
                raise ApiError(400, "before is not a finding of this run.")
            query = query.where(or_(findings.c.created_at < cursor.created_at,
                                    and_(findings.c.created_at == cursor.created_at, findings.c.id < cursor.id)))
        rows = conn.execute(query.order_by(findings.c.created_at.desc(), findings.c.id.desc()).limit(limit)).all()
    listed = [{"id": r.id, "path": r.path, "line": r.line, "severity": r.severity, "title": r.title,
               "body": r.body, "step_id": r.step_id, "created_at": r.created_at, "raised_by": r.raised_by,
               "decision": r.decision, "reason": r.reason, "fix_sha": r.fix_sha} for r in rows]
    return {"findings": listed, "next_before": rows[-1].id if len(rows) == limit else None}


OUTPUT_SHOWN_BYTES = 256 * 1024


def _shown_output(output: Any) -> Any:
    text = json.dumps(output, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) <= OUTPUT_SHOWN_BYTES:
        return output
    return {TRUNCATED: True, "value": text.encode("utf-8")[:OUTPUT_SHOWN_BYTES].decode("utf-8", "ignore")}


@router.get("/api/runs/{run_id}/outputs")
async def list_outputs(run_id: str, request: Request,
                       granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    """Each step's stored output by step key, for steps that have one. A very large output is cut to text."""
    def read() -> list[Any]:
        with server(request).engine.connect() as conn:
            return conn.execute(select(run_steps.c.step_key, run_steps.c.output)
                                .where(run_steps.c.run_id == run_id, run_steps.c.output.is_not(None))).all()

    rows = await asyncio.to_thread(read)
    return {"outputs": {r.step_key: _shown_output(r.output) for r in rows if r.output is not None}}


ARTIFACT_FIELDS = ("name", "step_id", "media_type", "size", "created_at")


def _artifact(row: Any, content: bool = False) -> dict[str, Any]:
    fields = (*ARTIFACT_FIELDS, "content") if content else ARTIFACT_FIELDS
    return {name: row[name] for name in fields}


@router.get("/api/runs/{run_id}/artifacts")
async def list_artifacts(run_id: str, request: Request,
                         granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    """Every artifact of the run, without content, ordered by name."""
    def read() -> list[dict[str, Any]]:
        columns = [run_artifacts.c[name] for name in ARTIFACT_FIELDS]
        with server(request).engine.connect() as conn:
            rows = conn.execute(select(*columns).where(run_artifacts.c.run_id == run_id)).all()
        return [dict(r._mapping) for r in rows]

    stored = {a["name"]: a for a in await asyncio.to_thread(read)}
    stored.update({name: _artifact(a) for name, a in _live(request).writer.pending_artifacts(run_id).items()})
    return {"artifacts": [stored[name] for name in sorted(stored)]}


@router.get("/api/runs/{run_id}/artifacts/{name}")
async def get_artifact(run_id: str, name: str, request: Request,
                       granted: Granted = Depends(require("viewer", by_run()))) -> dict[str, Any]:
    pending = _live(request).writer.pending_artifacts(run_id).get(name)
    if pending is not None:
        return {"artifact": _artifact(pending, content=True)}

    def read() -> Any:
        with server(request).engine.connect() as conn:
            return conn.execute(select(run_artifacts).where(run_artifacts.c.run_id == run_id,
                                                            run_artifacts.c.name == name)).first()

    row = await asyncio.to_thread(read)
    if row is None:
        raise ApiError(404, "No such artifact.")
    return {"artifact": _artifact(row._mapping, content=True)}
