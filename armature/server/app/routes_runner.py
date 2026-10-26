"""Runner keys, run registration, and HTTP event ingest.

Registration and ingest take machine credentials in ``Authorization``, never
a session, so they need no Origin or CSRF check.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from armature.server import runners
from armature.server.app.deps import Granted, by_team, require, server
from armature.server.app.http import ApiError, client_ip
from armature.server.app.routes_api import _check_name
from armature.server.auth import AuthError, count_call, ip_bucket
from armature.server.ingest import ACCEPTED, DUPLICATE, REJECTED, load_summary

router = APIRouter()

EVENTS_BODY_LIMIT = 1024 * 1024
REGISTER_LIMIT = 600
UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}
MAX_TOKENS_PER_POST = 4
LINE_TOKEN = re.compile(rb'"token"\s*:\s*"([^"]{1,200})"')


class KeyBody(BaseModel):
    name: str


def bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def live(request: Request):
    return request.app.state.live


def _list_keys(request: Request, team_id: str | None) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {"runner_keys": runners.list_keys(conn, team_id)}


def _create_key(request: Request, granted: Granted, team_id: str | None, body: KeyBody) -> dict[str, Any]:
    name = _check_name(body.name, "runner key")
    srv = server(request)
    try:
        with srv.engine.begin() as conn:
            record, key = runners.create_key(conn, srv.keyring, team_id=team_id, name=name,
                                             actor_id=granted.user_id, now=srv.now(), ip=client_ip(request))
    except IntegrityError as exc:
        raise ApiError(409, "Try again.") from exc
    return {"runner_key": record, "key": key}


def _delete_key(request: Request, granted: Granted, team_id: str | None, key_id: str) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.begin() as conn:
        if not runners.revoke_key(conn, team_id=team_id, key_id=key_id, actor_id=granted.user_id,
                                  now=srv.now(), ip=client_ip(request)):
            raise ApiError(404, "No such runner key.")
    live(request).forget_tokens()
    return {"ok": True}


@router.get("/api/teams/{team_id}/runner-keys")
def list_runner_keys(request: Request, granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    return _list_keys(request, granted.target.team_id)


@router.post("/api/teams/{team_id}/runner-keys")
def create_runner_key(body: KeyBody, request: Request,
                      granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    return _create_key(request, granted, granted.target.team_id, body)


@router.post("/api/teams/{team_id}/runner-keys/{key_id}/delete")
def delete_runner_key(key_id: str, request: Request,
                      granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    return _delete_key(request, granted, granted.target.team_id, key_id)


@router.get("/api/runner-keys")
def list_instance_runner_keys(request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    return _list_keys(request, None)


@router.post("/api/runner-keys")
def create_instance_runner_key(body: KeyBody, request: Request,
                               granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    return _create_key(request, granted, None, body)


@router.post("/api/runner-keys/{key_id}/delete")
def delete_instance_runner_key(key_id: str, request: Request,
                               granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    return _delete_key(request, granted, None, key_id)


@router.post("/api/runner/runs")
async def register_run(request: Request) -> dict[str, Any]:
    srv = server(request)
    presented = bearer(request)

    def check():
        with srv.engine.begin() as conn:
            return runners.authenticate(conn, srv.keyring, presented, srv.now())

    key = await asyncio.to_thread(check)
    if key is None:
        raise ApiError(401, "A valid runner key is required.", UNAUTHORIZED)
    try:
        await asyncio.to_thread(count_call, srv.engine, key=f"runner:{key.id}", limit=REGISTER_LIMIT, now=srv.now())
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc
    try:
        body = await request.json()
    except ValueError as exc:
        raise ApiError(400, "The body must be a JSON object.") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "The body must be a JSON object.")
    ip = client_ip(request)

    def register():
        with srv.engine.begin() as conn:
            row, token = runners.register_run(conn, srv.keyring, key=key, body=body, now=srv.now(), ip=ip)
            return load_summary(conn, row["id"]), token

    try:
        try:
            summary, token = await asyncio.to_thread(register)
        except IntegrityError:
            # Two first runs of an instance key can race to create the operations team.
            summary, token = await asyncio.to_thread(register)
    except runners.RegistrationError as exc:
        raise ApiError(exc.status, exc.message) from exc
    except IntegrityError as exc:
        raise ApiError(409, "Try again.") from exc
    hub = live(request)
    hub.run_started(summary)
    cfg = srv.cfg
    return {"run_id": summary["id"], "token": token,
            "events": {"unix": hub.unix_endpoint, "http": f"{cfg.public_origin}{cfg.base_path}/api/events"}}


@router.post("/api/events")
async def post_events(request: Request) -> JSONResponse:
    hub = live(request)
    if not hub.running:
        raise ApiError(503, "Event ingest is not running.")
    if not hub.ip_limit.allow(ip_bucket(client_ip(request))):
        raise ApiError(429, "Too many event posts from this address.", {"Retry-After": "1"})
    token = bearer(request)
    body = await request.body()
    # Each new token can cost a database lookup, so one request may not try many.
    tokens = set(LINE_TOKEN.findall(body)) | ({token.encode()} if token else set())
    if len(tokens) > MAX_TOKENS_PER_POST:
        raise ApiError(400, f"A post may carry at most {MAX_TOKENS_PER_POST} event tokens.")
    counts = {ACCEPTED: 0, REJECTED: 0, DUPLICATE: 0}
    for index, line in enumerate(body.splitlines()):
        if not line.strip():
            continue
        counts[await hub.ingest(line, token)] += 1
        if index % 200 == 199:
            await asyncio.sleep(0)
    return JSONResponse({"accepted": counts[ACCEPTED], "rejected": counts[REJECTED],
                         "duplicates": counts[DUPLICATE]}, status_code=202)
