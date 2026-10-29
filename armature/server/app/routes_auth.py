"""Session, password sign-in, invites, step-up, and the signed-in user's own account."""
from __future__ import annotations

import hashlib
import hmac
import secrets as _random
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import Connection, select

from armature.server import audit, auth, invites, slacklink
from armature.server.app.deps import Server, current_session, server, signed_in
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import ABSOLUTE, AuthError, Session
from armature.server.db.schema import memberships, projects, teams, users
from armature.server.passwords import check_password_policy

router = APIRouter()

SLACK_CODES_PER_HOUR = 5


class LoginBody(BaseModel):
    username: Any = None
    password: Any = None
    totp: Any = None


class StepUpBody(BaseModel):
    password: Any = None
    totp: Any = None


class PasswordBody(BaseModel):
    current: Any = None
    new: str


class InviteBody(BaseModel):
    token: Any = None


class AcceptBody(BaseModel):
    token: Any = None
    password: str
    code: Any = None


def _memberships(conn: Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(memberships.c.scope_kind, memberships.c.scope_id, memberships.c.role,
               teams.c.name.label("team_name"), projects.c.name.label("project_name"))
        .select_from(memberships
                     .outerjoin(teams, (memberships.c.scope_kind == "team") & (teams.c.id == memberships.c.scope_id))
                     .outerjoin(projects, (memberships.c.scope_kind == "project")
                                & (projects.c.id == memberships.c.scope_id)))
        .where(memberships.c.user_id == user_id).order_by(memberships.c.scope_kind, memberships.c.scope_id)
    ).all()
    return [{"scope_kind": r.scope_kind, "scope_id": r.scope_id,
             "scope_name": r.team_name if r.scope_kind == "team" else r.project_name, "role": r.role} for r in rows]


def login_csrf(srv: Server, pre_token: str) -> str:
    return hmac.new(srv.keyring.subkey("sessions"), b"login-csrf." + pre_token.encode(), hashlib.sha256).hexdigest()


def session_payload(srv: Server, session: Session | None, csrf: str | None = None) -> dict[str, Any]:
    methods = {"password": srv.cfg.password_login, "oidc": srv.cfg.oidc is not None, "totp": srv.cfg.require_totp}
    if session is None:
        return {"user": None, "csrf": csrf, "memberships": [], "auth": methods, "sso": False}
    user = session.user
    with srv.engine.connect() as conn:
        listed = _memberships(conn, user.id)
    return {
        "user": {"id": user.id, "username": user.username, "display_name": user.display_name,
                 "is_admin": bool(user.is_admin), "slack_workspace_id": user.slack_workspace_id,
                 "slack_user_id": user.slack_user_id},
        "csrf": session.csrf, "memberships": listed, "auth": methods, "sso": session.sso,
    }


def set_session_cookie(srv: Server, response: Response, token: str) -> None:
    response.set_cookie(srv.cookie_name, token, max_age=int(ABSOLUTE.total_seconds()), path=srv.cookie_path,
                        secure=not srv.insecure_localhost, httponly=True, samesite="strict")


def clear_session_cookie(srv: Server, response: Response) -> None:
    response.set_cookie(srv.cookie_name, "", max_age=0, path=srv.cookie_path, secure=not srv.insecure_localhost,
                        httponly=True, samesite="strict")


def _raise(exc: AuthError) -> None:
    raise ApiError(exc.status, exc.message, exc.headers) from exc


# A signed-out visitor gets a CSRF token bound to a pre-session cookie, which the sign-in POST must carry.
@router.get("/api/session")
def get_session(request: Request, response: Response) -> dict[str, Any]:
    srv = server(request)
    session = current_session(request)
    if session is not None:
        return session_payload(srv, session)
    pre = request.cookies.get(srv.pre_cookie_name)
    if not auth.valid_token(pre):
        pre = _random.token_urlsafe(32)
        response.set_cookie(srv.pre_cookie_name, pre, max_age=int(ABSOLUTE.total_seconds()), path=srv.cookie_path,
                            secure=not srv.insecure_localhost, httponly=True, samesite="strict")
    return session_payload(srv, None, login_csrf(srv, pre))


def check_signed_out_mutation(request: Request) -> None:
    """Origin and CSRF for a POST that may come before any session: the pre-session token, or the session's."""
    srv = server(request)
    if request.headers.get("origin") != srv.cfg.public_origin:
        raise ApiError(403, "Origin not allowed.")
    provided = request.headers.get("x-csrf-token")
    pre = request.cookies.get(srv.pre_cookie_name)
    existing = current_session(request)
    pre_ok = auth.valid_token(pre) and auth.safe_equal(provided, login_csrf(srv, pre))
    if not pre_ok and not (existing is not None and auth.safe_equal(provided, existing.csrf)):
        raise ApiError(403, "Missing or invalid CSRF token.")


@router.post("/api/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    srv = server(request)
    check_signed_out_mutation(request)
    if not srv.cfg.password_login:
        raise ApiError(404, "Password sign-in is turned off.")
    try:
        signed = auth.login(srv.engine, srv.keyring, username=body.username, password=body.password, code=body.totp,
                            ip=client_ip(request), now=srv.now(), dummy=srv.dummy_hash,
                            old_token=request.cookies.get(srv.cookie_name), require_totp=srv.cfg.require_totp)
    except AuthError as exc:
        _raise(exc)
    set_session_cookie(srv, response, signed.token)
    with srv.engine.connect() as conn:
        session = auth.load_session(conn, srv.keyring, signed.token, srv.now(), touch=False)
    return session_payload(srv, session)


@router.post("/api/logout")
def logout(request: Request, response: Response, session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.begin() as conn:
        auth.end_session(conn, session.token_hmac)
        audit.record(conn, action="logout", actor_id=session.user_id, ip=client_ip(request))
    clear_session_cookie(srv, response)
    return {"ok": True}


@router.post("/api/step-up")
def step_up(body: StepUpBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    try:
        auth.step_up(srv.engine, srv.keyring, session, password=body.password, code=body.totp,
                     ip=client_ip(request), now=srv.now())
    except AuthError as exc:
        _raise(exc)
    return {"ok": True}


@router.post("/api/account/password")
def change_password(body: PasswordBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    try:
        check_password_policy(body.new)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        auth.change_password(srv.engine, session, current=body.current, new=body.new, ip=client_ip(request),
                             now=srv.now(), cost=srv.password_cost)
    except AuthError as exc:
        _raise(exc)
    return {"ok": True}


class SlackLinkBody(BaseModel):
    workspace_id: str
    user_id: str


class SlackCodeBody(BaseModel):
    code: str


@router.post("/api/account/slack/start")
def start_slack_link(body: SlackLinkBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    """Send a one-time code to the Slack account the user names, through a verified Slack connection of theirs."""
    srv = server(request)
    try:
        auth.count_call(srv.engine, key=f"slack-link:{session.user_id}", limit=SLACK_CODES_PER_HOUR, now=srv.now(),
                        window=timedelta(hours=1))
        return slacklink.start(srv.engine, srv.keyring, user_id=session.user_id, is_admin=bool(session.user.is_admin),
                               workspace_id=body.workspace_id, slack_user_id=body.user_id, now=srv.now(),
                               ip=client_ip(request), transport=request.app.state.vendor_transport)
    except AuthError as exc:
        _raise(exc)
    except slacklink.LinkError as exc:
        raise ApiError(exc.status, exc.message) from exc


@router.post("/api/account/slack/confirm")
def confirm_slack_link(body: SlackCodeBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    try:
        with srv.engine.begin() as conn:
            linked = slacklink.confirm(conn, srv.keyring, user_id=session.user_id, code=body.code, now=srv.now(),
                                       ip=client_ip(request))
    except slacklink.LinkError as exc:
        raise ApiError(exc.status, exc.message) from exc
    if not linked:
        raise ApiError(400, "That code doesn't match. Check the direct message from the bot.")
    with srv.engine.connect() as conn:
        row = conn.execute(select(users.c.slack_workspace_id, users.c.slack_user_id)
                           .where(users.c.id == session.user_id)).one()
    return {"slack_workspace_id": row.slack_workspace_id, "slack_user_id": row.slack_user_id}


@router.post("/api/invite/inspect")
def inspect_invite(body: InviteBody, request: Request) -> dict[str, Any]:
    srv = server(request)
    check_signed_out_mutation(request)
    try:
        found = invites.inspect(srv.engine, srv.keyring, body.token, ip=client_ip(request), now=srv.now(),
                                require_totp=srv.cfg.require_totp)
    except AuthError as exc:
        _raise(exc)
    return {"username": found.username, "otpauth_uri": found.otpauth_uri}


@router.post("/api/invite/accept")
def accept_invite(body: AcceptBody, request: Request, response: Response) -> dict[str, Any]:
    srv = server(request)
    check_signed_out_mutation(request)
    try:
        check_password_policy(body.password)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        accepted = invites.accept(srv.engine, srv.keyring, body.token, password=body.password, code=body.code,
                                  ip=client_ip(request), now=srv.now(), cost=srv.password_cost,
                                  require_totp=srv.cfg.require_totp)
    except AuthError as exc:
        _raise(exc)
    set_session_cookie(srv, response, accepted.token)
    with srv.engine.connect() as conn:
        session = auth.load_session(conn, srv.keyring, accepted.token, srv.now(), touch=False)
    return session_payload(srv, session)
