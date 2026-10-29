"""Read-only listing of installed packs and their adapters.

There are no write routes: a pack is code, so only an admin on the server host
installs one, with ``armature server pack install``.
"""
from __future__ import annotations

from pathlib import PurePath
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from armature.packs import Pack
from armature.server import packs as server_packs
from armature.server.app.deps import server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import Session
from armature.server.db.schema import packs

router = APIRouter()


def _recorded(request: Request) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {r.name: r for r in conn.execute(select(packs.c.name, packs.c.source, packs.c.pinned_ref)).all()}


# A directory install records a host path, so only its last segment is shown.
def _shown_source(row) -> str | None:
    if row is None:
        return None
    if row.pinned_ref.startswith("sha256:"):
        return PurePath(row.source).name
    return row.source


def _pack(pack: Pack, row) -> dict[str, Any]:
    return {
        "name": pack.name,
        "version": pack.version,
        "description": pack.description,
        "source": _shown_source(row),
        "pinned_ref": row.pinned_ref if row is not None else None,
        "adapters": [
            {"ref": pack.ref(name), "description": adapter.description, "params": adapter.params,
             "outputs": adapter.outputs, "timeout": adapter.timeout, "writes": adapter.writes,
             "secrets": adapter.secrets}
            for name, adapter in sorted(pack.adapters.items())
        ],
    }


@router.get("/api/packs")
def list_packs(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    catalog = server_packs.catalog(server(request).cfg.packs_dir)
    recorded = _recorded(request)
    # The directory name alone, so a viewer never learns the host's paths.
    errors = [{"pack": error.path.name, "message": error.message} for error in catalog.errors]
    return {"packs": [_pack(p, recorded.get(p.name)) for p in catalog.packs()], "errors": errors}


@router.get("/api/packs/{name}")
def get_pack(name: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    pack = server_packs.catalog(server(request).cfg.packs_dir).pack(name)
    if pack is None:
        raise ApiError(404, "Not found.")
    return {"pack": _pack(pack, _recorded(request).get(name))}
