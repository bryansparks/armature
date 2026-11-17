"""The team lead's API for tracked repositories, whose pull request history the server collects."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from armature.server import rbac
from armature.server.app.deps import Granted, by_team, require, server
from armature.server.app.http import ApiError, client_ip
from armature.server.insights import repos

router = APIRouter()


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except repos.RepoError as exc:
        raise ApiError(exc.status, exc.message) from exc


@router.get("/api/teams/{team_id}/repos")
def list_repos(request: Request, granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    team_id = granted.target.team_id
    with server(request).engine.connect() as conn:
        return {"repos": [repos.public(conn, r) for r in repos.listed(conn, team_id)],
                "connections": repos.github_connections(conn, team_id)}


@router.post("/api/teams/{team_id}/repos")
def track_repo(request: Request, payload: dict[str, Any] = Body(...),
               granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    srv = server(request)
    team_id = granted.target.team_id
    with _refusals():
        repo_id = repos.track(srv.engine, srv.keyring, team_id=team_id, body=payload, actor_id=granted.user_id,
                              now=srv.now(), ip=client_ip(request), transport=request.app.state.vendor_transport,
                              admin=rbac.at_least(granted.role, "admin"))
        with srv.engine.connect() as conn:
            return {"repo": repos.public(conn, repos.get(conn, team_id, repo_id))}


@router.post("/api/teams/{team_id}/repos/{repo_id}")
def update_repo(repo_id: str, request: Request, payload: dict[str, Any] = Body(...),
                granted: Granted = Depends(require("team_lead", by_team()))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = repos.update_repo(conn, repos.get(conn, granted.target.team_id, repo_id), body=payload,
                                actor_id=granted.user_id, now=srv.now(), ip=client_ip(request),
                                admin=rbac.at_least(granted.role, "admin"))
        return {"repo": repos.public(conn, row)}


@router.post("/api/teams/{team_id}/repos/{repo_id}/delete")
def untrack_repo(repo_id: str, request: Request,
                 granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        repos.remove(conn, repos.get(conn, granted.target.team_id, repo_id), actor_id=granted.user_id,
                     ip=client_ip(request))
    return {"ok": True}
