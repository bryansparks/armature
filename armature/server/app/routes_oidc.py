"""GET /auth/oidc/login and /auth/oidc/callback."""
from __future__ import annotations

import hmac
import html
import logging
import re

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError

from armature.server import audit, auth, oidc
from armature.server import secrets as secret_store
from armature.server.app.deps import Server, server
from armature.server.app.http import ApiError, client_ip
from armature.server.app.routes_auth import set_session_cookie
from armature.server.crypto import SealError

log = logging.getLogger(__name__)
router = APIRouter()


def _redirect_uri(srv: Server) -> str:
    return f"{srv.cfg.public_origin}{srv.cfg.base_path}/auth/oidc/callback"


def _state_key(srv: Server) -> bytes:
    return hmac.digest(srv.keyring.subkey("sessions"), b"oidc-state", "sha256")


def _state_path(srv: Server) -> str:
    return f"{srv.cfg.base_path}/auth/oidc"


def _clear_state(srv: Server, response: Response) -> None:
    response.set_cookie(srv.state_cookie_name, "", max_age=0, path=_state_path(srv),
                        secure=not srv.insecure_localhost, httponly=True, samesite="lax")


# A meta refresh is a same-site navigation, so the SameSite=Strict session cookie rides along on the next page.
def _go(srv: Server, path: str) -> HTMLResponse:
    target = html.escape(f"{srv.cfg.base_path}{path}", quote=True)
    body = f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={target}">'
    return HTMLResponse(body)


def _provider(srv: Server) -> oidc.Provider:
    if srv.provider is None:
        raise ApiError(404, "Single sign-on is not configured.")
    return srv.provider


RETURN_HASH = re.compile(r"^#/[A-Za-z0-9/_?=&.%+-]{0,200}$")


@router.get("/auth/oidc/login")
def oidc_login(request: Request, step_up: bool = False, return_to: str | None = Query(None, alias="return")) -> Response:
    srv = server(request)
    provider = _provider(srv)
    flow = oidc.new_flow()
    try:
        url = provider.authorization_url(redirect_uri=_redirect_uri(srv), state=flow["state"], nonce=flow["nonce"],
                                         verifier=flow["verifier"], fresh=step_up)
    except (httpx.HTTPError, oidc.OIDCError, KeyError, ValueError) as exc:
        log.warning("OIDC discovery failed: %s", exc)
        raise ApiError(502, "Single sign-on is unavailable.") from exc
    payload = {**flow, "fresh": step_up, "return": return_to if return_to and RETURN_HASH.fullmatch(return_to) else "",
               "exp": int(srv.now().timestamp()) + oidc.STATE_TTL}
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(srv.state_cookie_name, oidc.sign_state(_state_key(srv), payload), max_age=oidc.STATE_TTL,
                        path=_state_path(srv), secure=not srv.insecure_localhost, httponly=True, samesite="lax")
    return response


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str | None = None, state: str | None = None) -> Response:
    srv = server(request)
    provider = _provider(srv)
    ip = client_ip(request)
    now = srv.now()
    try:
        flow = oidc.open_state(_state_key(srv), request.cookies.get(srv.state_cookie_name), now.timestamp())
        if not state or not code or not hmac.compare_digest(state, str(flow.get("state", ""))):
            raise oidc.OIDCError("state does not match")
        with srv.engine.connect() as conn:
            client_secret = secret_store.resolve_instance(conn, srv.keyring, provider.cfg.client_secret_ref)
        if client_secret is None:
            raise oidc.OIDCError(f"instance secret {provider.cfg.client_secret_ref} is not set")
        token = provider.exchange(code=code, redirect_uri=_redirect_uri(srv), verifier=flow["verifier"],
                                  client_secret=client_secret)
        fresh = flow.get("fresh") is True
        claims = provider.verify_id_token(token["id_token"], nonce=flow["nonce"], now=now, fresh=fresh)
        session_token = _sign_in(srv, request, provider, claims, now=now, ip=ip, fresh=fresh)
    except (oidc.OIDCError, httpx.HTTPError, SealError, KeyError, ValueError, IntegrityError) as exc:
        log.warning("OIDC sign-in refused: %s", exc)
        with srv.engine.begin() as conn:
            audit.record(conn, action="login", actor_id=None, ip=ip, detail={"outcome": "refused", "method": "oidc"})
        response = _go(srv, "/login?error=sso")
        _clear_state(srv, response)
        return response
    response = _go(srv, "/" + str(flow.get("return") or ""))
    _clear_state(srv, response)
    set_session_cookie(srv, response, session_token)
    return response


def _sign_in(srv: Server, request: Request, provider: oidc.Provider, claims: dict, *, now, ip: str,
             fresh: bool) -> str:
    def attempt(allow_username: bool) -> str:
        with srv.engine.begin() as conn:
            user_id = oidc.sign_in(conn, provider.cfg, claims, now=now, ip=ip, allow_username=allow_username)
            old_token = request.cookies.get(srv.cookie_name)
            if auth.valid_token(old_token):
                auth.end_session(conn, auth.token_hmac(srv.keyring, old_token))
            token, _csrf = auth.start_session(conn, srv.keyring, user_id, now, ip=ip,
                                              step_up=fresh or not srv.cfg.require_totp)
            audit.record(conn, action="login", actor_id=user_id, ip=ip, target_kind="user", target_id=user_id,
                         detail={"outcome": "ok", "method": "oidc", "fresh": fresh})
        return token

    # Two first sign-ins can race for one username or one (iss, sub), so a conflict retries once without a name.
    try:
        return attempt(True)
    except IntegrityError:
        return attempt(False)
