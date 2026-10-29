"""The AWS page: the connection, environment types, Mac hosts, environments, and the warm runner. Admins only."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from armature.server import environments
from armature.server.app.deps import Granted, require, server, signed_in
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import Session
from armature.server.db.schema import environment_types

router = APIRouter()


def service(request: Request) -> environments.EnvironmentService:
    return request.app.state.environments


def _call(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except environments.EnvError as exc:
        raise ApiError(exc.status, exc.message) from exc


@router.get("/api/aws")
def get_aws(request: Request, granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    srv = server(request)
    with srv.engine.connect() as conn:
        row = environments.connection(conn)
        listed = environments.list_environments(conn)
        return {"connection": environments.public_connection(srv.keyring, row),
                "types": environments.list_types(conn), "hosts": environments.list_hosts(conn),
                "environments": listed,
                "warm": next((e for e in listed if e["purpose"] == "warm" and e["state"] in environments.LIVE), None),
                "status": service(request).snapshot()}


@router.post("/api/aws/connection")
def set_connection(request: Request, body: dict[str, Any] = Body(...),
                   granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    srv = server(request)
    try:
        with srv.engine.begin() as conn:
            row = _call(environments.set_connection, conn, srv.keyring, body, actor_id=granted.user_id, now=srv.now(),
                        ip=client_ip(request))
    except IntegrityError as exc:
        raise ApiError(409, "Try again.") from exc
    return {"connection": environments.public_connection(srv.keyring, row)}


@router.post("/api/aws/connection/verify")
def verify_connection(request: Request,
                      granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    row = _call(service(request).verify, actor_id=granted.user_id, ip=client_ip(request))
    return {"connection": environments.public_connection(server(request).keyring, row)}


def _set_type(request: Request, granted: Granted, body: dict[str, Any], type_id: str | None) -> dict[str, Any]:
    srv = server(request)
    try:
        with srv.engine.begin() as conn:
            row = _call(environments.set_type, conn, body, type_id=type_id, actor_id=granted.user_id, now=srv.now(),
                        ip=client_ip(request))
    except IntegrityError as exc:
        raise ApiError(409, "An environment type with that name exists.") from exc
    return {"type": environments.public_type(row)}


@router.post("/api/aws/types")
def create_type(request: Request, body: dict[str, Any] = Body(...),
                granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    return _set_type(request, granted, body, None)


@router.post("/api/aws/types/{type_id}")
def update_type(type_id: str, request: Request, body: dict[str, Any] = Body(...),
                granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    return _set_type(request, granted, body, type_id)


@router.post("/api/aws/environments/{env_id}/terminate")
def terminate_environment(env_id: str, request: Request,
                          granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    _call(service(request).terminate, env_id, actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


@router.post("/api/aws/warm/start")
def start_warm(request: Request, body: dict[str, Any] = Body(...),
               granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    env_id = _call(service(request).start_warm, body.get("type_id"), actor_id=granted.user_id,
                   ip=client_ip(request))
    return {"environment_id": env_id}


@router.post("/api/aws/warm/stop")
def stop_warm(request: Request, granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    _call(service(request).stop_warm, actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


@router.post("/api/aws/hosts")
def allocate_host(request: Request, body: dict[str, Any] = Body(...),
                  granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    try:
        row = _call(service(request).allocate_host, body, actor_id=granted.user_id, ip=client_ip(request))
    except IntegrityError as exc:
        raise ApiError(409, "A host with that name exists.") from exc
    return {"host": environments.public_host(row)}


@router.post("/api/aws/hosts/{host_id}/instance")
def set_host_instance(host_id: str, request: Request, body: dict[str, Any] = Body(...),
                      granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    row = _call(service(request).set_host_instance, host_id, body.get("instance_id"), actor_id=granted.user_id,
                ip=client_ip(request))
    return {"host": environments.public_host(row)}


@router.post("/api/aws/hosts/{host_id}/release")
def release_host(host_id: str, request: Request,
                 granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    _call(service(request).release, host_id, actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/api/environment-types")
def list_type_names(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    """The names a project's environment type picker offers. Image and cost details stay on the AWS page."""
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(environment_types.c.id, environment_types.c.name, environment_types.c.kind,
                                   environment_types.c.capabilities, environment_types.c.enabled)
                            .order_by(environment_types.c.name)).all()
    return {"types": [{"id": r.id, "name": r.name, "kind": r.kind, "capabilities": r.capabilities or [],
                       "enabled": bool(r.enabled)} for r in rows]}
