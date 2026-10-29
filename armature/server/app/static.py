"""The browser UI: index.html at {base}/, login.html at {base}/login, invite.html at {base}/invite, assets under {base}/static/."""
from __future__ import annotations

import html
import re

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from armature.server.app.deps import current_session, server
from armature.server.app.http import ApiError

router = APIRouter()

ASSET = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.(js|css|svg|png|ico|woff2)$")
BASE_META = re.compile(rb'<meta name="armature-base" content="[^"]*">')
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
         ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}


def _page(request: Request, name: str) -> Response:
    srv = server(request)
    path = srv.static_dir / name
    if not path.is_file():
        raise ApiError(404, "Not found.")
    meta = f'<meta name="armature-base" content="{html.escape(srv.cfg.base_path, quote=True)}">'.encode()
    body = BASE_META.sub(lambda _m: meta, path.read_bytes(), count=1)
    return Response(body, media_type="text/html; charset=utf-8")


@router.get("/")
def index(request: Request) -> Response:
    if current_session(request) is None:
        return RedirectResponse(f"{server(request).cfg.base_path}/login", status_code=302)
    return _page(request, "index.html")


@router.get("/login")
def login_page(request: Request) -> Response:
    return _page(request, "login.html")


@router.get("/invite")
def invite_page(request: Request) -> Response:
    return _page(request, "invite.html")


@router.get("/static/{relative:path}")
def asset(relative: str, request: Request) -> Response:
    root = server(request).static_dir.resolve()
    path = (root / relative).resolve()
    if not ASSET.fullmatch(relative) or not path.is_relative_to(root) or not path.is_file():
        raise ApiError(404, "Not found.")
    return Response(path.read_bytes(), media_type=TYPES[path.suffix])
