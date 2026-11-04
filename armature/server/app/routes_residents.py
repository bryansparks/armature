"""Residents over HTTP: reading an instance, asking it, signaling it, and the admin acts.

Reading is a pure database read — a dashboard may poll it without spending a turn — while
ask and signal enqueue events that turn. ``resident.enabled`` gates those two, once the
caller's access to the workflow is proven and before any row is written, so the feature
is inert until it is turned on and never leaks its state to a stranger; the admin actions
only write rows and stay available either way, as reading does. Every call is audited with
the signed-in user as its actor (``resident.read|ask|signal|pause|resume|close|upgrade``).
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, func, select, update

from armature.server import audit, conversations, defs, rbac, residents
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import AuthError, Session, count_call
from armature.server.db.schema import resident_events, workflows
from armature.server.db.schema import residents as resident_rows
from armature.spec.models import ResidentCfg

router = APIRouter()

READS_PER_HOUR = 240  # a dashboard's poll; reading spends nothing but writes an audit row
ASKS_PER_HOUR = 60    # the conversations' message cap: every ask and signal turns a run
LISTED = 50
EVENTS_LIMIT_MAX = 200
INSTANCES_MAX = 500
WAIT_MAX_S = 60
WAIT_DEFAULT_S = 30  # ?wait=0 or less: the caller asked to wait but not for how long
POLL_S = 0.2
# The shape a bare custom name allows — the same vocabulary resident.on's custom.<name>
# keys accept (spec/validator.py) — never the prefixed kind itself. Defined in residents.py,
# where the emit tool enforces the same grammar.
NAME_RE = residents.NAME_RE


class AskBody(BaseModel):
    question: str
    wait: int | None = None


class SignalBody(BaseModel):
    name: str | None = None
    payload: Any = None


class CloseBody(BaseModel):
    reason: str


def _throttle(srv: Any, session: Session, key: str, limit: int) -> None:
    """``count_call`` is two transactions: call it from a worker thread, never the loop."""
    try:
        count_call(srv.engine, key=key, limit=limit, now=srv.now(), window=timedelta(hours=1))
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc


def _workflow(conn: Connection, session: Session, workflow_id: str, min_role: str) -> Any:
    """The workflow the resident lives under, with the caller's role on its scope checked."""
    access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
    try:
        row = defs.readable_row(conn, defs.WORKFLOW, workflow_id, access)
    except defs.DefError as exc:
        raise ApiError(exc.status, exc.message) from exc
    if not rbac.at_least(rbac.role_for(access, rbac.Target(team_id=row.team_id,
                                                           project_id=row.project_id)), min_role):
        raise ApiError(403, "Not allowed.")
    return row


def _instance(conn: Connection, workflow_id: str, instance_key: str) -> Any:
    resident = conn.execute(select(resident_rows).where(
        resident_rows.c.workflow_id == workflow_id,
        resident_rows.c.instance_key == instance_key)).first()
    if resident is None:
        raise ApiError(404, "No such resident instance.")
    return resident


def _locked_instance(conn: Connection, workflow_id: str, instance_key: str) -> Any:
    """The instance row held under the transaction's lock: a wake committed between an act's
    check and its write would re-pin a live turn, so the lock comes before the checks."""
    resident = conn.execute(select(resident_rows).where(
        resident_rows.c.workflow_id == workflow_id,
        resident_rows.c.instance_key == instance_key).with_for_update()).first()
    if resident is None:
        raise ApiError(404, "No such resident instance.")
    return resident


def _published(conn: Connection, resident: Any, keys: list[str] | None = None) -> dict[str, Any]:
    """The instance's published keys: what the spec it is pinned to allows `read` to serve."""
    spec = residents._spec(conn, resident)
    publish = spec.state.publish if spec is not None else []
    wanted = [key for key in (keys if keys is not None else publish) if key in publish]
    data = resident.data if isinstance(resident.data, dict) else {}
    return {key: data[key] for key in wanted if key in data}


def _public(conn: Connection, resident: Any, published: dict[str, Any]) -> dict[str, Any]:
    inbox = conn.execute(select(func.count()).select_from(resident_events).where(
        resident_events.c.resident_id == resident.id,
        resident_events.c.handled.is_(False))).scalar_one()
    return {"id": resident.id, "instance_key": resident.instance_key, "state": resident.state,
            "close_reason": resident.close_reason, "closed_at": resident.closed_at,
            "workflow_version": resident.workflow_version, "data": published,
            "data_version": resident.data_version, "turns": resident.turns, "inbox": inbox,
            "active_job_id": resident.active_job_id, "last_turn_at": resident.last_turn_at,
            "last_event_at": resident.last_event_at, "created_at": resident.created_at,
            "spent_usd_today": resident.spent_usd_day / 1_000_000}


def _event(row: Any) -> dict[str, Any]:
    return {"seq": row.seq, "kind": row.kind, "name": row.name, "payload": row.payload,
            "source": row.source, "reply_to": row.reply_to, "reply": row.reply,
            "dedupe_key": row.dedupe_key, "dry_run": row.dry_run, "handled": row.handled,
            "attempts": row.attempts, "handled_by_job": row.handled_by_job,
            "created_at": row.created_at}


def _check_enabled(conn: Connection) -> None:
    """The rollback gate: with ``resident.enabled`` off the API takes no turn and writes no row."""
    if not residents.enabled(conn):
        raise ApiError(503, "Residents are not enabled on this instance.")


def _checked_key(instance_key: str) -> str:
    """An instance key the column can hold: an over-length key can name no instance, so it answers
    with the 404 of an unknown one — and never reaches ``ensure``, whose insert it would break."""
    if len(instance_key) > residents.KEY_MAX:
        raise ApiError(404, "No such resident instance.")
    return instance_key


def _check_text(value: Any, what: str) -> str:
    try:
        return conversations.check_text(value, what)
    except conversations.ConversationError as exc:
        raise ApiError(exc.status, exc.message) from exc


def _checked_name(name: str | None) -> str | None:
    """A bare custom name in the on-key vocabulary; the prefixed kind itself is refused."""
    if not name:
        return None
    if name.startswith("custom.") or not NAME_RE.fullmatch(name):
        raise ApiError(400, "name is a bare custom name: 1-64 of lowercase letters, "
                            "digits, '.', '_' or '-'.")
    return name


@router.get("/api/residents/{workflow_id}")
def list_instances(workflow_id: str, request: Request,
                   session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    _throttle(srv, session, f"resident:{session.user_id}", READS_PER_HOUR)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "viewer")
        rows = conn.execute(select(resident_rows).where(resident_rows.c.workflow_id == row.id)
                            .order_by(resident_rows.c.created_at.desc(), resident_rows.c.instance_key)
                            .limit(INSTANCES_MAX)).all()
        audit.record(conn, action="resident.read", actor_id=session.user_id, team_id=row.team_id,
                     target_kind="workflow", target_id=row.id)
        return {"residents": [_public(conn, r, _published(conn, r)) for r in rows]}


@router.get("/api/residents/{workflow_id}/{instance_key}/read")
def read(instance_key: str, workflow_id: str, request: Request,
         keys: str | None = Query(None, max_length=191),
         session: Session = Depends(signed_in)) -> dict[str, Any]:
    """The instance's published metrics, straight from the row: no job, no run, no cost."""
    srv = server(request)
    _throttle(srv, session, f"resident:{session.user_id}", READS_PER_HOUR)
    wanted = [key.strip() for key in keys.split(",") if key.strip()] if keys else None
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "viewer")
        resident = _instance(conn, row.id, instance_key)
        audit.record(conn, action="resident.read", actor_id=session.user_id,
                     team_id=resident.team_id, target_kind="resident", target_id=resident.id)
        return {"resident": _public(conn, resident, _published(conn, resident, wanted))}


# The singleton registration pins the empty key as a fixed argument: ``instance_key`` is not
# a parameter of the partial, so a query param of that name binds nothing and can never
# alias the bare path onto a keyed instance.
router.get("/api/residents/{workflow_id}/read")(partial(read, ""))


def _ask(srv: Any, session: Session, workflow_id: str, instance_key: str, question: str) \
        -> tuple[str, int, str | None]:
    _throttle(srv, session, f"resident_ask:{session.user_id}", ASKS_PER_HOUR)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "contributor")
        _check_enabled(conn)  # after access: a stranger learns nothing about the feature's state
        resident = residents.ensure(conn, team_id=row.team_id, project_id=row.project_id,
                                    workflow_id=row.id, instance_key=_checked_key(instance_key),
                                    now=srv.now())
        try:
            seq = residents.enqueue(conn, resident.id, "ask", payload={"text": question},
                                     source={"user_id": session.user_id},
                                     reply_to={"channel": "api", "user_id": session.user_id},
                                     now=srv.now())
        except residents.ResidentError as exc:
            raise ApiError(exc.status, exc.message) from exc
        job_id = conn.execute(select(resident_rows.c.active_job_id).where(
            resident_rows.c.id == resident.id)).scalar()
        audit.record(conn, action="resident.ask", actor_id=session.user_id, team_id=resident.team_id,
                     target_kind="resident", target_id=resident.id, detail={"seq": seq})
        return resident.id, seq, job_id


def _read_reply(engine: Any, resident_id: str, seq: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        return conn.execute(select(resident_events.c.reply).where(
            resident_events.c.resident_id == resident_id,
            resident_events.c.seq == seq)).scalar()


async def _await_reply(engine: Any, resident_id: str, seq: int, seconds: int) -> dict[str, Any] | None:
    """Poll the asking event's row for the turn's reply until the caller's wait runs out.

    ``resident.reply`` is a database write the live broker never carries, so the wait reads
    the row the tool wrote — the asker collects the answer from the event that asked. None
    means the turn did not answer in time; the caller polls the event for it later.
    """
    deadline = time.monotonic() + seconds
    while True:
        reply = await asyncio.to_thread(_read_reply, engine, resident_id, seq)
        if reply is not None:
            return reply
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(POLL_S)


@router.post("/api/residents/{workflow_id}/{instance_key}/ask")
async def ask(instance_key: str, workflow_id: str, body: AskBody, request: Request,
              wait: int | None = Query(None),
              session: Session = Depends(signed_in)) -> dict[str, Any]:
    """Enqueue an ask the instance's turn answers; ``wait`` blocks for the reply."""
    question = _check_text(body.question, "question")
    srv = server(request)
    resident_id, seq, job_id = await asyncio.to_thread(
        _ask, srv, session, workflow_id, instance_key, question)
    seconds = wait if wait is not None else body.wait
    if seconds is None:
        return {"event_seq": seq, "job_id": job_id}  # async: poll .../events/{seq} for the reply
    return {"event_seq": seq, "reply": await _await_reply(
        srv.engine, resident_id, seq, min(WAIT_MAX_S, seconds if seconds > 0 else WAIT_DEFAULT_S))}


router.post("/api/residents/{workflow_id}/ask")(partial(ask, ""))


@router.post("/api/residents/{workflow_id}/{instance_key}/signal")
def signal(instance_key: str, workflow_id: str, body: SignalBody, request: Request,
           session: Session = Depends(signed_in)) -> dict[str, Any]:
    """A plain signal, or a custom one when ``name`` gives its bare name."""
    srv = server(request)
    _throttle(srv, session, f"resident_ask:{session.user_id}", ASKS_PER_HOUR)
    name = _checked_name(body.name)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "contributor")
        _check_enabled(conn)  # after access: a stranger learns nothing about the feature's state
        resident = residents.ensure(conn, team_id=row.team_id, project_id=row.project_id,
                                    workflow_id=row.id, instance_key=_checked_key(instance_key),
                                    now=srv.now())
        try:
            seq = residents.enqueue(conn, resident.id, f"custom.{name}" if name else "signal",
                                     payload=body.payload, source={"user_id": session.user_id},
                                     now=srv.now())
        except residents.ResidentError as exc:
            raise ApiError(exc.status, exc.message) from exc
        job_id = conn.execute(select(resident_rows.c.active_job_id).where(
            resident_rows.c.id == resident.id)).scalar()
        audit.record(conn, action="resident.signal", actor_id=session.user_id,
                     team_id=resident.team_id, target_kind="resident", target_id=resident.id,
                     detail={"seq": seq, "name": name})
        return {"event_seq": seq, "job_id": job_id}


router.post("/api/residents/{workflow_id}/signal")(partial(signal, ""))


@router.get("/api/residents/{workflow_id}/{instance_key}/events")
def events(instance_key: str, workflow_id: str, request: Request,
           unhandled: int = Query(0, ge=0, le=1), limit: int = Query(LISTED, ge=1, le=EVENTS_LIMIT_MAX),
           session: Session = Depends(signed_in)) -> dict[str, Any]:
    """The instance's inbox, newest first — or, with ``unhandled``, the waiting events oldest first."""
    srv = server(request)
    _throttle(srv, session, f"resident:{session.user_id}", READS_PER_HOUR)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "viewer")
        resident = _instance(conn, row.id, instance_key)
        query = select(resident_events).where(resident_events.c.resident_id == resident.id)
        if unhandled:
            query = query.where(resident_events.c.handled.is_(False)).order_by(resident_events.c.seq)
        else:
            query = query.order_by(resident_events.c.seq.desc())
        rows = conn.execute(query.limit(limit)).all()
        audit.record(conn, action="resident.read", actor_id=session.user_id,
                     team_id=resident.team_id, target_kind="resident", target_id=resident.id)
        return {"events": [_event(r) for r in rows]}


router.get("/api/residents/{workflow_id}/events")(partial(events, ""))


@router.get("/api/residents/{workflow_id}/{instance_key}/events/{seq}")
def one_event(instance_key: str, workflow_id: str, seq: int, request: Request,
              session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    _throttle(srv, session, f"resident:{session.user_id}", READS_PER_HOUR)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "viewer")
        resident = _instance(conn, row.id, instance_key)
        event = conn.execute(select(resident_events).where(
            resident_events.c.resident_id == resident.id,
            resident_events.c.seq == seq)).first()
        if event is None:
            raise ApiError(404, "No such event.")
        audit.record(conn, action="resident.read", actor_id=session.user_id,
                     team_id=resident.team_id, target_kind="resident", target_id=resident.id,
                     detail={"seq": seq})
        return {"event": _event(event)}


router.get("/api/residents/{workflow_id}/events/{seq}")(partial(one_event, ""))


# The admin acts: they only write rows, so they work whether the feature is enabled or not.

@router.post("/api/residents/{workflow_id}/{instance_key}/pause")
def pause(instance_key: str, workflow_id: str, request: Request,
          session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "admin")
        resident = _locked_instance(conn, row.id, instance_key)
        if resident.state == "closed":
            raise ApiError(409, "instance is closed")
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            state="paused", close_reason="admin", updated_at=srv.now()))
        audit.record(conn, action="resident.pause", actor_id=session.user_id, team_id=resident.team_id,
                     target_kind="resident", target_id=resident.id, detail={"reason": "admin"})
        return {"resident": _public(conn, _instance(conn, row.id, instance_key),
                                    _published(conn, resident))}


router.post("/api/residents/{workflow_id}/pause")(partial(pause, ""))


@router.post("/api/residents/{workflow_id}/{instance_key}/resume")
def resume(instance_key: str, workflow_id: str, request: Request,
           session: Session = Depends(signed_in)) -> dict[str, Any]:
    """A paused instance back to idle, then woken: an event that waited out the pause turns."""
    srv = server(request)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "admin")
        resident = _locked_instance(conn, row.id, instance_key)
        if resident.state != "paused":
            raise ApiError(409, "instance is not paused")
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            state="idle", close_reason=None, updated_at=srv.now()))
        audit.record(conn, action="resident.resume", actor_id=session.user_id,
                     team_id=resident.team_id, target_kind="resident", target_id=resident.id)
        residents.wake(conn, resident.id, srv.now())
        return {"resident": _public(conn, _instance(conn, row.id, instance_key),
                                    _published(conn, resident))}


router.post("/api/residents/{workflow_id}/resume")(partial(resume, ""))


@router.post("/api/residents/{workflow_id}/{instance_key}/close")
def close(instance_key: str, workflow_id: str, body: CloseBody, request: Request,
          session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    reason = _check_text(body.reason, "reason")
    if len(reason) > 32:  # the column's width: refuse the write, never truncate silently
        raise ApiError(422, "reason is longer than the 32 characters a resident keeps.")
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "admin")
        resident = _locked_instance(conn, row.id, instance_key)
        if resident.state == "closed":
            raise ApiError(409, "instance is closed")
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(
            state="closed", close_reason=reason, closed_at=srv.now(), updated_at=srv.now()))
        audit.record(conn, action="resident.close", actor_id=session.user_id, team_id=resident.team_id,
                     target_kind="resident", target_id=resident.id, detail={"reason": reason})
        return {"resident": _public(conn, _instance(conn, row.id, instance_key),
                                    _published(conn, resident))}


router.post("/api/residents/{workflow_id}/close")(partial(close, ""))


def _current_spec(conn: Connection, workflow_id: str) -> tuple[int, ResidentCfg | None]:
    """The workflow's current version and the resident config it carries, or None."""
    version = conn.execute(select(workflows.c.current_version).where(
        workflows.c.id == workflow_id)).scalar()
    block = residents._resident_block(defs.body_of(conn, defs.WORKFLOW, workflow_id, version))
    if block is None:
        return version, None
    try:
        return version, ResidentCfg.model_validate(block)
    except ValidationError:
        return version, None


@router.post("/api/residents/{workflow_id}/{instance_key}/upgrade")
def upgrade(instance_key: str, workflow_id: str, request: Request,
            session: Session = Depends(signed_in)) -> dict[str, Any]:
    """Re-pin the instance to the workflow's current version (docs: Version pinning).

    The instance's data must satisfy the new version's state schema: a version that
    rejects it is pinned with the instance paused for a migrate — never closed. A turn
    in flight refuses the move: the pin never changes under a live turn.
    """
    srv = server(request)
    with srv.engine.begin() as conn:
        row = _workflow(conn, session, workflow_id, "admin")
        resident = _locked_instance(conn, row.id, instance_key)
        if resident.state == "closed":
            raise ApiError(409, "instance is closed")
        if resident.active_job_id is not None:
            raise ApiError(409, "A turn is in flight; upgrade it when the instance is at rest.")
        version, spec = _current_spec(conn, row.id)
        if spec is None:
            raise ApiError(409, "The workflow's current version has no resident block.")
        schema_failed = False
        try:
            residents._check_state(spec.state, resident.data if isinstance(resident.data, dict) else {})
        except residents.ResidentError:
            schema_failed = True
        values: dict[str, Any] = {"workflow_version": version, "updated_at": srv.now()}
        if schema_failed:
            values.update(state="paused", close_reason="state_schema")
        elif resident.state == "paused" and resident.close_reason == "state_schema":
            values.update(state="idle", close_reason=None)  # the migrate landed: it may turn again
        conn.execute(update(resident_rows).where(resident_rows.c.id == resident.id).values(**values))
        audit.record(conn, action="resident.upgrade", actor_id=session.user_id,
                     team_id=resident.team_id, target_kind="resident", target_id=resident.id,
                     detail={"version": version, "state_schema": schema_failed})
        if not schema_failed and values.get("state") == "idle":
            residents.wake(conn, resident.id, srv.now())
        return {"resident": _public(conn, _instance(conn, row.id, instance_key),
                                    _published(conn, resident))}


router.post("/api/residents/{workflow_id}/upgrade")(partial(upgrade, ""))