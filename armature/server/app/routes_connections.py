"""Connections: members read them, and leads add, change, verify, and delete them with a fresh step-up."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from armature.server import connections, rbac
from armature.server.app.deps import Granted, by_project, by_team, require, server
from armature.server.app.http import ApiError, client_ip

router = APIRouter()


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except connections.ConnError as exc:
        raise ApiError(exc.status, exc.message) from exc
    except IntegrityError as exc:
        raise ApiError(409, "A connection with that name exists here.") from exc


def _by_connection(conn: Connection, params: dict[str, Any]) -> rbac.Target:
    try:
        row = connections.get(conn, params["connection_id"])
    except connections.ConnError as exc:
        raise rbac.NotFound(exc.message) from exc
    return rbac.Target(team_id=row.team_id, project_id=row.project_id)


# A project lead writes the project's connections. A team's connections still need a team lead.
def _writer(granted: Granted) -> None:
    if granted.target.project_id is None and not rbac.at_least(granted.role, "team_lead"):
        raise ApiError(403, "Not allowed.")


def _list(request: Request, scope: str) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        return {"connections": [connections.public(conn, srv.keyring, r) for r in connections.listed(conn, scope)]}


def _set(request: Request, granted: Granted, body: dict[str, Any], row: Any | None) -> dict[str, Any]:
    srv = server(request)
    target = granted.target
    with _refusals(), srv.engine.begin() as conn:
        saved = connections.set_connection(conn, srv.keyring, team_id=target.team_id, project_id=target.project_id,
                                           row=row, body=body, actor_id=granted.user_id, now=srv.now(),
                                           ip=client_ip(request), is_admin=granted.access.is_admin)
        return {"connection": connections.public(conn, srv.keyring, saved)}


@router.get("/api/teams/{team_id}/connections")
def list_team_connections(request: Request,
                          granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    return _list(request, granted.target.team_id)


@router.get("/api/projects/{project_id}/connections")
def list_project_connections(request: Request,
                             granted: Granted = Depends(require("viewer", by_project()))) -> dict[str, Any]:
    return _list(request, granted.target.project_id)


@router.post("/api/teams/{team_id}/connections")
def create_team_connection(request: Request, body: dict[str, Any] = Body(...),
                           granted: Granted = Depends(require("team_lead", by_team(), step_up=True))
                           ) -> dict[str, Any]:
    return _set(request, granted, body, None)


@router.post("/api/projects/{project_id}/connections")
def create_project_connection(request: Request, body: dict[str, Any] = Body(...),
                              granted: Granted = Depends(require("project_lead", by_project(), step_up=True))
                              ) -> dict[str, Any]:
    return _set(request, granted, body, None)


@router.post("/api/connections/{connection_id}")
def update_connection(connection_id: str, request: Request, body: dict[str, Any] = Body(...),
                      granted: Granted = Depends(require("project_lead", _by_connection, step_up=True))
                      ) -> dict[str, Any]:
    _writer(granted)
    with _refusals(), server(request).engine.connect() as conn:
        row = connections.get(conn, connection_id)
    return _set(request, granted, body, row)


@router.post("/api/connections/{connection_id}/verify")
def verify_connection(connection_id: str, request: Request,
                      granted: Granted = Depends(require("project_lead", _by_connection, step_up=True))
                      ) -> dict[str, Any]:
    _writer(granted)
    srv = server(request)
    with _refusals():
        row = connections.verify(srv.engine, srv.keyring, connection_id, actor_id=granted.user_id,
                                 ip=client_ip(request), now=srv.now(), transport=request.app.state.vendor_transport)
    with srv.engine.connect() as conn:
        return {"connection": connections.public(conn, srv.keyring, row)}


@router.post("/api/connections/{connection_id}/delete")
def delete_connection(connection_id: str, request: Request,
                      granted: Granted = Depends(require("project_lead", _by_connection, step_up=True))
                      ) -> dict[str, Any]:
    _writer(granted)
    with _refusals(), server(request).engine.begin() as conn:
        connections.remove(conn, connections.get(conn, connection_id), actor_id=granted.user_id,
                           ip=client_ip(request))
    return {"ok": True}
