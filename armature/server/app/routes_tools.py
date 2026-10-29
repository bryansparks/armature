"""The tool broker's API: a run's step trades the run token for a grant, then calls built-in tools with it.

Each route takes a machine credential in ``Authorization``, never a session, so it needs no Origin or CSRF check.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from armature.server import broker
from armature.server.app.deps import server
from armature.server.app.http import ApiError, client_ip
from armature.server.app.routes_runner import NO_STORE, UNAUTHORIZED, bearer, json_object, live

router = APIRouter()

CALLS_PER_S = 5.0
CALLS_BURST = 50
TOOLS_PATH = "/api/tools/"
# A review carries its findings and the last findings state, so it may run well past the default limit.
TOOLS_BODY_LIMIT = 2 * 1024 * 1024


def _public_url(request: Request) -> str:
    cfg = server(request).cfg
    return f"{cfg.public_origin}{cfg.base_path}"


async def _guarded(request: Request, work) -> JSONResponse:
    token = bearer(request)
    if not request.app.state.tool_calls.allow((token or "")[:64]):
        raise ApiError(429, "Too many tool calls from this step.", {"Retry-After": "1"})
    try:
        result = await asyncio.to_thread(work, token)
    except broker.BrokerError as exc:
        raise ApiError(exc.status, exc.message, UNAUTHORIZED if exc.status == 401 else None) from exc
    return JSONResponse(result, headers=NO_STORE)


@router.post("/api/tools/grants")
async def grant(request: Request) -> JSONResponse:
    srv = server(request)
    body = await json_object(request)

    def issue(token: str | None) -> dict[str, Any]:
        with srv.engine.begin() as conn:
            return broker.issue_grant(conn, srv.keyring, run_token=token, body=body, now=srv.now(),
                                      ip=client_ip(request))

    return await _guarded(request, issue)


@router.post("/api/tools/git-credential")
async def git_credential(request: Request) -> JSONResponse:
    srv = server(request)
    body = await json_object(request)
    return await _guarded(request, lambda token: broker.git_credential(
        srv.engine, srv.keyring, public_url=_public_url(request), token=token, body=body, now=srv.now(),
        ip=client_ip(request), transport=request.app.state.vendor_transport))


@router.post("/api/tools/{tool}")
async def call_tool(tool: str, request: Request) -> JSONResponse:
    srv = server(request)
    body = await json_object(request)
    return await _guarded(request, lambda token: broker.call_tool(
        srv.engine, srv.keyring, public_url=_public_url(request), token=token, tool=tool, params=body.get("params"),
        now=srv.now(), ip=client_ip(request), transport=request.app.state.vendor_transport,
        packs_dir=srv.cfg.packs_dir, hub=live(request)))
