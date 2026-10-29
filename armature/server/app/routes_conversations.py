"""Web conversations: a chat with a conversation workflow, one short turn on a runner per message.

The server records each message and queues a turn. It never calls a model
itself. A web conversation is its owner's, and a Slack one shows to anyone who
can see its team or project.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import Connection, and_, or_, select

from armature.server import conversations, defs, rbac
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import AuthError, Session, count_call
from armature.server.db.schema import conversations as conversation_rows

router = APIRouter()

MESSAGES_PER_HOUR = 60
LISTED = 50


class StartBody(BaseModel):
    workflow_id: str
    project_id: str | None = None
    text: str


class MessageBody(BaseModel):
    text: str


def _access(conn: Connection, session: Session) -> rbac.Access:
    return rbac.load_access(conn, session.user_id, bool(session.user.is_admin))


def _sees(access: rbac.Access, user_id: str, conv: Any) -> bool:
    if conv.kind == "web":
        return conv.owner_id == user_id or access.is_admin
    return rbac.role_for(access, rbac.Target(team_id=conv.team_id, project_id=conv.project_id)) is not None


def _visible(conn: Connection, session: Session, conversation_id: str) -> Any:
    try:
        conv = conversations.get(conn, conversation_id)
    except conversations.ConversationError as exc:
        raise ApiError(404, "Not found.") from exc
    if not _sees(_access(conn, session), session.user_id, conv):
        raise ApiError(404, "Not found.")
    return conv


def _throttle(request: Request, session: Session) -> None:
    srv = server(request)
    try:
        count_call(srv.engine, key=f"conversation:{session.user_id}", limit=MESSAGES_PER_HOUR, now=srv.now(),
                   window=timedelta(hours=1))
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc


@router.get("/api/conversations")
def list_conversations(request: Request, thread: str | None = Query(None, max_length=191),
                       session: Session = Depends(signed_in)) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        access = _access(conn, session)
        shared = and_(conversation_rows.c.kind != "web", rbac.scope_clause(
            rbac.visible_scope(access), conversation_rows.c.team_id, conversation_rows.c.project_id))
        query = select(conversation_rows).where(or_(shared, conversation_rows.c.owner_id == session.user_id))
        if thread is not None:
            query = query.where(conversation_rows.c.thread == thread)
        rows = conn.execute(query.order_by(conversation_rows.c.updated_at.desc()).limit(LISTED)).all()
        return {"conversations": [{k: v for k, v in conversations.public(conn, r).items() if k != "messages"}
                                  for r in rows]}


@router.post("/api/conversations")
def start(body: StartBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    """Open a web conversation with a workflow the user may run, and queue its first turn."""
    srv = server(request)
    _throttle(request, session)
    try:
        with srv.engine.begin() as conn:
            access = _access(conn, session)
            row = defs.readable_row(conn, defs.WORKFLOW, body.workflow_id, access)
            project_id = body.project_id or row.project_id
            target = rbac.project_target(conn, project_id) if project_id else (
                rbac.Target(team_id=row.team_id) if row.team_id else None)
            if target is None:
                raise ApiError(400, "Pick the project this conversation belongs to.")
            if (row.project_id and project_id != row.project_id) or (row.team_id and target.team_id != row.team_id):
                raise ApiError(400, "The project is not where this workflow belongs.")
            if not rbac.at_least(rbac.role_for(access, target), "contributor"):
                raise ApiError(403, "Not allowed.")
            if conversations.gated(conn, row.id):
                raise ApiError(400, conversations.GATED)
            conversation_id = conversations.open_conversation(
                conn, team_id=target.team_id, project_id=target.project_id, workflow_id=row.id, kind="web",
                thread=None, now=srv.now(), owner_id=session.user_id)
            conversations.accept(conn, conversation_id, author=session.user_id,
                                 text=conversations.check_text(body.text), now=srv.now())
            return {"conversation": conversations.public(conn, conversations.get(conn, conversation_id))}
    except (defs.DefError, conversations.ConversationError) as exc:
        raise ApiError(exc.status, exc.message) from exc
    except rbac.NotFound as exc:
        raise ApiError(404, "Not found.") from exc


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request,
                     session: Session = Depends(signed_in)) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {"conversation": conversations.public(conn, _visible(conn, session, conversation_id))}


@router.post("/api/conversations/{conversation_id}/messages")
def post_message(conversation_id: str, body: MessageBody, request: Request,
                 session: Session = Depends(signed_in)) -> dict[str, Any]:
    """A message in the user's own web conversation. A Slack thread is answered in Slack."""
    srv = server(request)
    _throttle(request, session)
    try:
        with srv.engine.begin() as conn:
            conv = _visible(conn, session, conversation_id)
            if conv.kind != "web" or conv.owner_id != session.user_id:
                raise ApiError(403, "Only its owner writes in a web conversation.")
            if conv.state != "open":
                raise ApiError(409, "This conversation is closed. Start a new one.")
            conversations.accept(conn, conv.id, author=session.user_id, text=conversations.check_text(body.text),
                                 now=srv.now())
            return {"conversation": conversations.public(conn, conversations.get(conn, conv.id))}
    except conversations.ConversationError as exc:
        raise ApiError(exc.status, exc.message) from exc
