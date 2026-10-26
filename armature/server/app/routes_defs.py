"""Workflows, documents, providers, and settings.

A team's definitions show to anyone who can see the team, a project's to
anyone who can see the project, and instance-level ones to anyone signed in.
Settings are admin-only. The rules for who may write live in
``armature.server.defs``.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, StrictInt
from sqlalchemy import Connection, and_, func, select
from sqlalchemy.exc import IntegrityError

from armature.server import defs, rbac
from armature.server.app.deps import (
    Granted,
    by_team,
    require,
    require_step_up,
    server,
    signed_in,
)
from armature.server.app.http import ApiError, client_ip
from armature.server.auth import Session
from armature.server.db.schema import (
    DOCUMENT_KINDS,
    INSTANCE_SCOPE,
    providers,
    settings,
    users,
    versions,
)

router = APIRouter()

INSTANCE_NAMES = ("instance", INSTANCE_SCOPE)
# JSON escaping can roughly double a body, so the request limit sits above the stored cap.
DEFS_BODY_LIMIT = 2 * defs.BODY_MAX + 64 * 1024
DEFS_PATHS = ("/api/workflows", "/api/workflows/", "/api/documents", "/api/documents/")


class CreateBody(BaseModel):
    name: str | None = None
    key: str | None = None
    kind: str | None = None
    scope_kind: str
    scope_id: str | None = None
    body: str
    note: str | None = None


class SaveBody(BaseModel):
    body: str
    note: str | None = None
    base_version: StrictInt


class ValidateBody(BaseModel):
    body: str
    kind: str | None = None
    workflow_id: str | None = None
    document_id: str | None = None


class ForkBody(BaseModel):
    scope_kind: str
    scope_id: str | None = None
    name: str | None = None


class RollbackBody(BaseModel):
    version: StrictInt
    note: str | None = None
    base_version: StrictInt | None = None


class ParseBody(BaseModel):
    body: str


class RenderBody(BaseModel):
    body: str
    stages: list[dict[str, Any]]


class SettingBody(BaseModel):
    scope: str | None = None
    key: str
    value: Any = None


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except defs.DefError as exc:
        raise ApiError(exc.status, exc.message, extra=exc.extra) from exc
    except rbac.NotFound as exc:
        raise ApiError(404, str(exc)) from exc
    except IntegrityError as exc:
        raise ApiError(409, "That name is taken in this scope.") from exc


def _catalog(request: Request) -> Any:
    return defs.load_catalog(server(request).cfg.packs_dir)


def _load_access(conn: Connection, session: Session) -> rbac.Access:
    return rbac.load_access(conn, session.user_id, bool(session.user.is_admin))


def _history(conn, object_kind: str, object_id: str, current: int) -> tuple[dict[str, Any] | None, list]:
    rows = conn.execute(
        select(versions.c.version, versions.c.body, versions.c.note, versions.c.created_at,
               func.coalesce(users.c.username, versions.c.author_id).label("author"))
        .select_from(versions.outerjoin(users, users.c.id == versions.c.author_id))
        .where(versions.c.object_kind == object_kind, versions.c.object_id == object_id)
        .order_by(versions.c.version.desc())).all()
    listed = [{"version": r.version, "note": r.note, "author": r.author, "created_at": r.created_at} for r in rows]
    shown = next(({**item, "body": r.body} for item, r in zip(listed, rows, strict=True) if r.version == current),
                 None)
    return shown, listed


def _item(conn: Connection, kind: defs.Kind, row: Any, access: rbac.Access) -> dict[str, Any]:
    item = defs.summary(kind, row, access, defs.scope_names(conn))
    if kind is defs.WORKFLOW:
        item["lint_ok"] = defs.lint_ok(conn.execute(select(versions.c.note).where(and_(
            versions.c.object_kind == "workflow", versions.c.object_id == row.id,
            versions.c.version == row.current_version))).scalar())
    return item


def _list(request: Request, session: Session, kind: defs.Kind, team: str | None, project: str | None,
          doc_kind: str | None = None) -> list[dict[str, Any]]:
    extra = (versions.c.note,) if kind is defs.WORKFLOW else None
    with _refusals(), server(request).engine.connect() as conn:
        access = _load_access(conn, session)
        return defs.listing(conn, kind, access, team=team, project=project, doc_kind=doc_kind, extra=extra)


def _detail(request: Request, session: Session, kind: defs.Kind, object_id: str) -> dict[str, Any]:
    with _refusals(), server(request).engine.connect() as conn:
        access = _load_access(conn, session)
        row = defs.readable_row(conn, kind, object_id, access)
        version, history = _history(conn, kind.object_kind, row.id, row.current_version)
        item = _item(conn, kind, row, access)
        listed = defs.listing(conn, kind, access, doc_kind=getattr(row, "kind", None))
        item["effective_for"] = next((entry["effective_for"] for entry in listed if entry["id"] == row.id), [])
    return {kind.object_kind: item, "version": version, "versions": history, "editable": item["editable"]}


def _version(request: Request, session: Session, kind: defs.Kind, object_id: str, version: int) -> dict[str, Any]:
    with _refusals(), server(request).engine.connect() as conn:
        row = defs.readable_row(conn, kind, object_id, _load_access(conn, session))
        detail = defs.version_detail(conn, kind, row.id, version)
    if detail is None:
        raise ApiError(404, "No such version.")
    return {"version": detail}


def _diff(request: Request, session: Session, kind: defs.Kind, object_id: str, start: int | None,
          end: int | None) -> dict[str, Any]:
    with _refusals(), server(request).engine.connect() as conn:
        row = defs.readable_row(conn, kind, object_id, _load_access(conn, session))
        return {"diff": defs.diff(conn, kind, row, start, end)}


def _validate(request: Request, session: Session, kind: defs.Kind, payload: ValidateBody) -> dict[str, Any]:
    object_id = payload.workflow_id if kind is defs.WORKFLOW else payload.document_id
    doc_kind = payload.kind
    with _refusals(), server(request).engine.connect() as conn:
        access = _load_access(conn, session)
        previous = None
        if object_id:
            row = defs.readable_row(conn, kind, object_id, access)
            previous = defs.body_of(conn, kind, row.id, row.current_version)
            doc_kind = getattr(row, "kind", doc_kind)
    if kind is defs.WORKFLOW:
        validation = defs.validate_workflow(payload.body, actor=access, previous_body=previous,
                                            catalog=_catalog(request))
    else:
        validation = defs.validate_document(doc_kind or "", payload.body, actor=access, previous_body=previous,
                                            catalog=_catalog(request))
    return {"validation": validation.to_dict()}


def _create(request: Request, session: Session, kind: defs.Kind, payload: CreateBody) -> dict[str, Any]:
    srv = server(request)
    name = payload.name if kind is defs.WORKFLOW else payload.key
    with _refusals(), srv.engine.begin() as conn:
        access = _load_access(conn, session)
        scope = defs.load_scope(conn, payload.scope_kind, payload.scope_id)
        defs.authorize_write(access, scope)
        require_step_up(request, session)
        row, validation = defs.create(conn, kind, access=access, scope=scope, name=name, body=payload.body,
                                      note=payload.note, actor_id=session.user_id, now=srv.now(),
                                      ip=client_ip(request), doc_kind=payload.kind,
                                      catalog=_catalog(request))
        return {kind.object_kind: _item(conn, kind, row, access),
                "version": defs.version_detail(conn, kind, row.id, row.current_version),
                "validation": validation.to_dict()}


def _save(request: Request, session: Session, kind: defs.Kind, object_id: str, payload: SaveBody) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        access = _load_access(conn, session)
        row = defs.readable_row(conn, kind, object_id, access)
        row, validation = defs.save(conn, kind, row, access=access, body=payload.body, note=payload.note,
                                    base_version=payload.base_version, actor_id=session.user_id, now=srv.now(),
                                    ip=client_ip(request), catalog=_catalog(request))
        return {kind.object_kind: _item(conn, kind, row, access),
                "version": defs.version_detail(conn, kind, row.id, row.current_version),
                "validation": validation.to_dict()}


def _fork(request: Request, session: Session, kind: defs.Kind, object_id: str, payload: ForkBody) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        access = _load_access(conn, session)
        row = defs.readable_row(conn, kind, object_id, access)
        dest = defs.load_scope(conn, payload.scope_kind, payload.scope_id)
        defs.authorize_write(access, dest)
        require_step_up(request, session)
        forked = defs.fork(conn, kind, row, access=access, dest=dest, name=payload.name, actor_id=session.user_id,
                           now=srv.now(), ip=client_ip(request), catalog=_catalog(request))
        return {kind.object_kind: _item(conn, kind, forked, access)}


def _rollback(request: Request, session: Session, kind: defs.Kind, object_id: str,
              payload: RollbackBody) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        access = _load_access(conn, session)
        row = defs.readable_row(conn, kind, object_id, access)
        defs.authorize_write(access, defs.row_scope(row))
        require_step_up(request, session)
        row = defs.rollback(conn, kind, row, access=access, version=payload.version, note=payload.note,
                            actor_id=session.user_id, now=srv.now(), ip=client_ip(request),
                            catalog=_catalog(request), base_version=payload.base_version)
        return {kind.object_kind: _item(conn, kind, row, access),
                "version": defs.version_detail(conn, kind, row.id, row.current_version)}


def _delete(request: Request, session: Session, kind: defs.Kind, object_id: str) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        access = _load_access(conn, session)
        row = defs.readable_row(conn, kind, object_id, access)
        defs.authorize_write(access, defs.row_scope(row))
        require_step_up(request, session)
        defs.remove(conn, kind, row, access=access, actor_id=session.user_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/api/workflows")
def list_workflows(request: Request, team: str | None = Query(None, max_length=36),
                   project: str | None = Query(None, max_length=36),
                   session: Session = Depends(signed_in)) -> dict[str, Any]:
    return {"workflows": _list(request, session, defs.WORKFLOW, team, project)}


@router.post("/api/workflows/validate")
def validate_workflow(payload: ValidateBody, request: Request,
                      session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _validate(request, session, defs.WORKFLOW, payload)


@router.post("/api/workflows/parse")
def parse_workflow(payload: ParseBody, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return defs.parse_stages(payload.body)


@router.post("/api/workflows/render")
def render_workflow(payload: RenderBody, session: Session = Depends(signed_in)) -> dict[str, Any]:
    with _refusals():
        return {"body": defs.render_stages(payload.body, payload.stages)}


@router.post("/api/workflows", status_code=201)
def create_workflow(payload: CreateBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _create(request, session, defs.WORKFLOW, payload)


@router.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _detail(request, session, defs.WORKFLOW, workflow_id)


@router.get("/api/workflows/{workflow_id}/versions/{version}")
def get_workflow_version(workflow_id: str, version: int, request: Request,
                         session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _version(request, session, defs.WORKFLOW, workflow_id, version)


@router.get("/api/workflows/{workflow_id}/diff")
def diff_workflow(workflow_id: str, request: Request, start: int | None = Query(None, alias="from"),
                  end: int | None = Query(None, alias="to"), session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _diff(request, session, defs.WORKFLOW, workflow_id, start, end)


@router.post("/api/workflows/{workflow_id}")
def save_workflow(workflow_id: str, payload: SaveBody, request: Request,
                  session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _save(request, session, defs.WORKFLOW, workflow_id, payload)


@router.post("/api/workflows/{workflow_id}/fork", status_code=201)
def fork_workflow(workflow_id: str, payload: ForkBody, request: Request,
                  session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _fork(request, session, defs.WORKFLOW, workflow_id, payload)


@router.post("/api/workflows/{workflow_id}/rollback")
def rollback_workflow(workflow_id: str, payload: RollbackBody, request: Request,
                      session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _rollback(request, session, defs.WORKFLOW, workflow_id, payload)


@router.post("/api/workflows/{workflow_id}/delete")
def delete_workflow(workflow_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _delete(request, session, defs.WORKFLOW, workflow_id)


@router.get("/api/documents")
def list_documents(request: Request, kind: str | None = Query(None), team: str | None = Query(None, max_length=36),
                   project: str | None = Query(None, max_length=36),
                   session: Session = Depends(signed_in)) -> dict[str, Any]:
    if kind is not None and kind not in DOCUMENT_KINDS:
        raise ApiError(400, f"kind must be one of {', '.join(DOCUMENT_KINDS)}.")
    return {"documents": _list(request, session, defs.DOCUMENT, team, project, kind)}


@router.post("/api/documents/validate")
def validate_document(payload: ValidateBody, request: Request,
                      session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _validate(request, session, defs.DOCUMENT, payload)


@router.post("/api/documents", status_code=201)
def create_document(payload: CreateBody, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _create(request, session, defs.DOCUMENT, payload)


@router.get("/api/documents/{document_id}")
def get_document(document_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _detail(request, session, defs.DOCUMENT, document_id)


@router.get("/api/documents/{document_id}/versions/{version}")
def get_document_version(document_id: str, version: int, request: Request,
                         session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _version(request, session, defs.DOCUMENT, document_id, version)


@router.get("/api/documents/{document_id}/diff")
def diff_document(document_id: str, request: Request, start: int | None = Query(None, alias="from"),
                  end: int | None = Query(None, alias="to"), session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _diff(request, session, defs.DOCUMENT, document_id, start, end)


@router.post("/api/documents/{document_id}")
def save_document(document_id: str, payload: SaveBody, request: Request,
                  session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _save(request, session, defs.DOCUMENT, document_id, payload)


@router.post("/api/documents/{document_id}/fork", status_code=201)
def fork_document(document_id: str, payload: ForkBody, request: Request,
                  session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _fork(request, session, defs.DOCUMENT, document_id, payload)


@router.post("/api/documents/{document_id}/rollback")
def rollback_document(document_id: str, payload: RollbackBody, request: Request,
                      session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _rollback(request, session, defs.DOCUMENT, document_id, payload)


@router.post("/api/documents/{document_id}/delete")
def delete_document(document_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    return _delete(request, session, defs.DOCUMENT, document_id)


@router.get("/api/teams/{team_id}/providers")
def list_providers(request: Request, granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    team_id = granted.target.team_id
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(providers).where(providers.c.team_id == team_id)
                            .order_by(providers.c.kind, providers.c.created_at)).all()
        configured = {r.id: defs.provider_configured(conn, r) for r in rows}
    listed = []
    for r in rows:
        config = r.config if isinstance(r.config, dict) else {}
        listed.append({"id": r.id, "kind": r.kind, "config": config, "aliases": r.aliases or {},
                       "configured": configured[r.id]})
    return {"providers": listed}


def _by_provider(conn: Connection, params: dict[str, Any]) -> rbac.Target:
    return defs.provider_target(conn, params["provider_id"])


@router.post("/api/teams/{team_id}/providers")
def create_provider(request: Request, payload: dict[str, Any] = Body(...),
                    granted: Granted = Depends(require("team_lead", by_team(), step_up=True))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = defs.create_provider(conn, team_id=granted.target.team_id, body=payload, actor_id=granted.user_id,
                                   now=srv.now(), ip=client_ip(request), is_admin=granted.access.is_admin)
    return {"provider": defs.provider_public(row)}


@router.post("/api/providers/{provider_id}")
def update_provider(provider_id: str, request: Request, payload: dict[str, Any] = Body(...),
                    granted: Granted = Depends(require("team_lead", _by_provider, step_up=True))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = defs.update_provider(conn, provider_id=provider_id, body=payload, actor_id=granted.user_id,
                                   now=srv.now(), ip=client_ip(request), is_admin=granted.access.is_admin)
    return {"provider": defs.provider_public(row)}


@router.post("/api/providers/{provider_id}/delete")
def delete_provider(provider_id: str, request: Request,
                    granted: Granted = Depends(require("team_lead", _by_provider, step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        defs.delete_provider(conn, provider_id=provider_id, actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/api/settings")
def list_settings(request: Request, scope: str | None = Query(None, max_length=36),
                  granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    query = select(settings.c.key, settings.c.value, settings.c.scope).order_by(settings.c.key)
    if scope:
        query = query.where(settings.c.scope == (INSTANCE_SCOPE if scope in INSTANCE_NAMES else scope))
    with server(request).engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"settings": [{"key": r.key, "value": r.value, "scope": r.scope} for r in rows]}


@router.post("/api/settings")
def put_setting(payload: SettingBody, request: Request,
                granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        team_id = defs.setting_scope(conn, payload.scope)
        setting = defs.put_setting(conn, team_id=team_id, key=payload.key, value=payload.value,
                                   actor_id=granted.user_id, now=srv.now(), ip=client_ip(request))
    return {"setting": setting}


@router.post("/api/settings/delete")
def delete_setting(payload: SettingBody, request: Request,
                   granted: Granted = Depends(require("admin", step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        team_id = defs.setting_scope(conn, payload.scope)
        defs.delete_setting(conn, team_id=team_id, key=payload.key, actor_id=granted.user_id, ip=client_ip(request))
    return {"ok": True}
