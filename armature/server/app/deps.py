"""Request dependencies: the signed-in session, mutation checks, and ``require``."""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import Connection, Engine

from armature.server import rbac
from armature.server.app.http import ApiError
from armature.server.auth import Session, load_session, safe_equal
from armature.server.config import ServerConfig
from armature.server.crypto import Keyring
from armature.server.oidc import Provider

COOKIE = "__Secure-armature"
LOCAL_COOKIE = "armature-local"
PRE_COOKIE = "__Secure-armature-pre"
LOCAL_PRE_COOKIE = "armature-pre-local"
STATE_COOKIE = "__Secure-armature-oidc"
LOCAL_STATE_COOKIE = "armature-oidc-local"
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
PRIVATE_HTTP_ORIGIN = re.compile(r"^http://(\d{1,3}(?:\.\d{1,3}){3})(:\d+)?$")


def _private_http_origin(origin: str) -> bool:
    match = PRIVATE_HTTP_ORIGIN.fullmatch(origin)
    if not match:
        return False
    try:
        return ipaddress.ip_address(match.group(1)).is_private
    except ValueError:
        return False
STEP_UP_REQUIRED = "step_up_required"


@dataclass
class Server:
    cfg: ServerConfig
    engine: Engine
    keyring: Keyring
    clock: Callable[[], datetime]
    dummy_hash: str
    static_dir: Path
    provider: Provider | None = None
    password_cost: int | None = None

    # Plain-http localhost drops Secure, which the __Secure- prefix would then forbid.
    @property
    def insecure_localhost(self) -> bool:
        if LOCAL_ORIGIN.fullmatch(self.cfg.public_origin):
            return True
        return self.cfg.allow_private_http and _private_http_origin(self.cfg.public_origin)

    @property
    def cookie_name(self) -> str:
        return LOCAL_COOKIE if self.insecure_localhost else COOKIE

    @property
    def pre_cookie_name(self) -> str:
        return LOCAL_PRE_COOKIE if self.insecure_localhost else PRE_COOKIE

    @property
    def state_cookie_name(self) -> str:
        return LOCAL_STATE_COOKIE if self.insecure_localhost else STATE_COOKIE

    @property
    def cookie_path(self) -> str:
        return self.cfg.base_path or "/"

    def now(self) -> datetime:
        return self.clock()


def server(request: Request) -> Server:
    return request.app.state.server


def current_session(request: Request) -> Session | None:
    if not hasattr(request.state, "session"):
        srv = server(request)
        with srv.engine.begin() as conn:
            request.state.session = load_session(conn, srv.keyring, request.cookies.get(srv.cookie_name), srv.now())
    return request.state.session


def check_mutation(request: Request, session: Session | None) -> None:
    """Every mutation needs POST, this instance's Origin, and the session's CSRF token."""
    if request.headers.get("origin") != server(request).cfg.public_origin:
        raise ApiError(403, "Origin not allowed.")
    if session is None or not safe_equal(request.headers.get("x-csrf-token"), session.csrf):
        raise ApiError(403, "Missing or invalid CSRF token.")


def signed_in(request: Request) -> Session:
    session = current_session(request)
    if session is None:
        raise ApiError(401, "Sign in first.")
    if request.method not in ("GET", "HEAD"):
        check_mutation(request, session)
    return session


def require_step_up(request: Request, session: Session) -> None:
    if not session.has_step_up(server(request).now()):
        raise ApiError(403, STEP_UP_REQUIRED)


@dataclass(frozen=True)
class Granted:
    session: Session
    access: rbac.Access
    target: rbac.Target
    role: str

    @property
    def user_id(self) -> str:
        return self.session.user_id


ScopeFrom = Callable[[Connection, dict[str, Any]], rbac.Target]


def by_team(param: str = "team_id") -> ScopeFrom:
    return lambda conn, params: rbac.team_target(conn, params[param])


def by_project(param: str = "project_id") -> ScopeFrom:
    return lambda conn, params: rbac.project_target(conn, params[param])


def by_run(param: str = "run_id") -> ScopeFrom:
    return lambda conn, params: rbac.run_target(conn, params[param])


def by_membership(param: str = "membership_id") -> ScopeFrom:
    return lambda conn, params: rbac.membership_target(conn, params[param])


def team_of(scope_from: ScopeFrom) -> ScopeFrom:
    """The team that owns what ``scope_from`` loads, for checks that need a team role even on a project."""
    def load(conn: Connection, params: dict[str, Any]) -> rbac.Target:
        return rbac.Target(team_id=scope_from(conn, params).team_id)
    return load


def grant(srv: Server, session: Session, target: rbac.Target, min_role: str, conn: Connection) -> Granted:
    access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
    role = rbac.role_for(access, target)
    if role is None:
        hidden = target.team_id is not None and not rbac.sees_team(access, target.team_id)
        raise ApiError(404 if hidden else 403, "Not found." if hidden else "Not allowed.")
    if not rbac.at_least(role, min_role):
        raise ApiError(403, "Not allowed.")
    return Granted(session=session, access=access, target=target, role=role)


def require(min_role: str, scope_from: ScopeFrom | None = None, *, step_up: bool = False):
    """One dependency per route: load the target from the database, then check the user's role on it.

    With no ``scope_from`` the target is the instance, which only an admin holds.
    """
    def dependency(request: Request, session: Session = Depends(signed_in)) -> Granted:
        srv = server(request)
        with srv.engine.connect() as conn:
            try:
                target = scope_from(conn, request.path_params) if scope_from else rbac.INSTANCE
            except rbac.NotFound as exc:
                raise ApiError(404, str(exc)) from exc
            granted = grant(srv, session, target, min_role, conn)
        if step_up:
            require_step_up(request, session)
        return granted

    return dependency
