"""Test runs of a stored workflow in stub mode."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from armature.server import dryrun, rbac
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import Session

router = APIRouter()


class DryRunBody(BaseModel):
    mode: str = "stub"
    from_run: str | None = None
    outputs: dict[str, Any] | None = None
    inputs: dict[str, Any] | None = None
    team_id: str | None = None
    missing: str | None = None


@router.post("/api/workflows/{workflow_id}/test")
async def test_workflow(workflow_id: str, payload: DryRunBody, request: Request,
                        session: Session = Depends(signed_in)) -> dict[str, Any]:
    srv = server(request)
    hub = request.app.state.live
    endpoint = dryrun.events_endpoint(hub, srv.cfg)
    ip = client_ip(request)

    def launch_run() -> dryrun.Launch:
        dryrun.check_mode(payload.mode)
        binary = dryrun.armature_binary()
        if binary is None:
            raise dryrun.DryRunError(503, "The armature command is not installed on this server.")
        with srv.engine.connect() as conn:
            access = rbac.load_access(conn, session.user_id, bool(session.user.is_admin))
        dryrun.ensure_lock_row(srv.engine)
        with srv.engine.begin() as conn:
            launch, args, missing_empty = dryrun.prepare(
                conn, srv.keyring, access=access, user_id=session.user_id, username=session.user.username,
                workflow_id=workflow_id, payload=payload.model_dump(), now=srv.now(), ip=ip,
                max_active=srv.cfg.dry_run.max_active)
        dryrun.start(binary, launch, args, dryrun.child_env(launch, endpoint, srv.cfg.packs_dir, missing_empty), hub,
                     memory_mb=srv.cfg.dry_run.memory_mb)
        return launch

    try:
        launch = await asyncio.to_thread(launch_run)
    except dryrun.DryRunError as exc:
        raise ApiError(exc.status, exc.message) from exc
    hub.run_started(launch.summary)
    return {"run_id": launch.run_id}
