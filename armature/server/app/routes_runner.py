"""Runner keys, run registration, the runner job API, and HTTP event ingest.

Registration and ingest take machine credentials in ``Authorization``, never
a session, so they need no Origin or CSRF check.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from armature.server import dispatch, environments, runners, worker
from armature.server.app.deps import Granted, by_team, require, server
from armature.server.app.http import ApiError, client_ip
from armature.server.app.routes_api import _check_name
from armature.server.auth import AuthError, count_call, ip_bucket
from armature.server.ingest import ACCEPTED, DUPLICATE, REJECTED, load_summary

router = APIRouter()

EVENTS_BODY_LIMIT = 1024 * 1024
JOBS_PATH = "/api/runner/jobs/"
# A finish report carries the checkpoint of a run that paused at a gate.
JOBS_BODY_LIMIT = 5 * 1024 * 1024
REGISTER_LIMIT = 600
# An instance enrolls once, so a handful per address is plenty, and guessing stays slow.
ENROLL_LIMIT = 30
# Claims and heartbeats come every few seconds, so they get an in-memory bucket per key, not a database row each.
POLL_PER_S = 5.0
POLL_BURST = 50
NO_STORE = {"Cache-Control": "no-store"}
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


async def runner_key(request: Request, *, counted: bool) -> Any:
    """The bearer runner key, rate limited per key: in the database when ``counted``, else in memory."""
    srv = server(request)
    presented = bearer(request)

    def check():
        with srv.engine.begin() as conn:
            return runners.authenticate(conn, srv.keyring, presented, srv.now())

    key = await asyncio.to_thread(check)
    if key is None:
        raise ApiError(401, "A valid runner key is required.", UNAUTHORIZED)
    if not counted:
        if not request.app.state.runner_polls.allow(key.id):
            raise ApiError(429, "Too many requests from this runner key.", {"Retry-After": "1"})
        return key
    try:
        await asyncio.to_thread(count_call, srv.engine, key=f"runner:{key.id}", limit=REGISTER_LIMIT, now=srv.now())
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc
    return key


async def json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ApiError(400, "The body must be a JSON object.") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "The body must be a JSON object.")
    return body


def events_url(request: Request) -> str:
    cfg = server(request).cfg
    return f"{cfg.public_origin}{cfg.base_path}/api/events"


@router.post("/api/runner/enroll")
async def runner_enroll(request: Request) -> JSONResponse:
    """Trade an environment's one-time enrollment token for its runner key. The token is the only credential."""
    srv = server(request)
    ip = client_ip(request)
    try:
        await asyncio.to_thread(count_call, srv.engine, key=f"enroll:{ip_bucket(ip)}", limit=ENROLL_LIMIT,
                                now=srv.now())
    except AuthError as exc:
        raise ApiError(exc.status, exc.message, exc.headers) from exc
    body = await json_object(request)

    def trade():
        with srv.engine.begin() as conn:
            return environments.enroll(conn, srv.keyring, token=body.get("token"), name=body.get("name"),
                                       now=srv.now(), ip=ip)

    try:
        key, _ = await asyncio.to_thread(trade)
    except environments.EnvError as exc:
        raise ApiError(exc.status, exc.message) from exc
    cfg = srv.cfg
    return JSONResponse({"key": key, "server": f"{cfg.public_origin}{cfg.base_path}"}, headers=NO_STORE)


@router.post("/api/runner/runs")
async def register_run(request: Request) -> dict[str, Any]:
    srv = server(request)
    key = await runner_key(request, counted=True)
    body = await json_object(request)
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
    return {"run_id": summary["id"], "token": token, "events": {"unix": hub.unix_endpoint, "http": events_url(request)}}


@router.post("/api/runner/hello")
async def runner_hello(request: Request) -> dict[str, Any]:
    srv = server(request)
    key = await runner_key(request, counted=True)
    body = await json_object(request)

    def record():
        with srv.engine.begin() as conn:
            return dispatch.hello(conn, key=key, body=body, now=srv.now())

    try:
        try:
            return await asyncio.to_thread(record)
        except IntegrityError:
            # Two hosts starting under one key and name race to insert the row.
            return await asyncio.to_thread(record)
    except dispatch.RunnerError as exc:
        raise ApiError(exc.status, exc.message) from exc
    except IntegrityError as exc:
        raise ApiError(409, "Try again.") from exc


@router.post("/api/runner/claim")
async def runner_claim(request: Request) -> Response:
    srv = server(request)
    key = await runner_key(request, counted=False)
    body = await json_object(request)
    try:
        bundle = await asyncio.to_thread(dispatch.claim, srv.engine, srv.keyring, key=key,
                                         runner_id=body.get("runner_id"), now=srv.now(), lease=worker.LEASE,
                                         events=events_url(request), bundle_version=body.get("bundle_version"))
    except dispatch.RunnerError as exc:
        raise ApiError(exc.status, exc.message) from exc
    if bundle is None:
        return Response(status_code=204, headers=NO_STORE)
    live(request).run_started(bundle.summary)
    return JSONResponse({"job": bundle.payload()}, headers=NO_STORE)


def _run_status(request: Request, run_id: str | None, stored: str | None) -> str | None:
    summary = live(request).summary(run_id) if run_id else None
    return summary["status"] if summary is not None else stored


@router.post("/api/runner/jobs/{job_id}/heartbeat")
async def runner_heartbeat(job_id: str, request: Request) -> dict[str, Any]:
    srv = server(request)
    key = await runner_key(request, counted=False)
    try:
        beat = await asyncio.to_thread(dispatch.heartbeat, srv.engine, key=key, job_id=job_id, now=srv.now(),
                                       lease=worker.LEASE)
    except dispatch.RunnerError as exc:
        raise ApiError(exc.status, exc.message) from exc
    return {"status": beat["status"], "cancel_requested": beat["cancel_requested"],
            "run_status": _run_status(request, beat["run_id"], beat["run_status"])}


@router.post("/api/runner/jobs/{job_id}/finish")
async def runner_finish(job_id: str, request: Request) -> dict[str, Any]:
    srv = server(request)
    key = await runner_key(request, counted=True)
    body = await json_object(request)
    hub = live(request)
    if body.get("status") == "suspended" and hub.running:
        # The gate must match the run_suspend event, so it has to be stored before the gate opens.
        await hub.writer.flush()
    try:
        run_id = await asyncio.to_thread(dispatch.run_of, srv.engine, key=key, job_id=job_id)
        status = await asyncio.to_thread(dispatch.finish, srv.engine, srv.keyring, key=key, job_id=job_id,
                                         body=body, now=srv.now(), live_status=_run_status(request, run_id, None))
    except dispatch.RunnerError as exc:
        raise ApiError(exc.status, exc.message) from exc
    return {"status": status}


@router.get("/api/runners")
def list_runners(request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        return {"runners": dispatch.list_runners(conn, srv.now())}


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
