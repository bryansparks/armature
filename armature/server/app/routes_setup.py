"""The first-run setup app: loopback only, one console token, and gone once an admin exists."""
from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

from armature.server import auth
from armature.server.app.deps import LOCAL_ORIGIN, Server
from armature.server.app.http import LOOPBACK_PEERS, ApiError, SecurityHeaders, install_error_handlers
from armature.server.app.routes_auth import set_session_cookie
from armature.server.app.static import BASE_META, TYPES
from armature.server.config import ServerConfig, load_config
from armature.server.crypto import MasterKeyError, keyring_from_source
from armature.server.db import make_engine, utcnow
from armature.server.passwords import PASSWORD_MIN
from armature.server.setup import SetupError, check_database_url, key_file_of, prepare, try_database, write_new_config
from armature.server.users import create_admin, has_admin

SETUP_HOST = "127.0.0.1"
TOKEN_HEADER = "x-setup-token"
FILES = ("setup.js", "style.css", "theme.js")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def setup_origin(cfg: ServerConfig) -> str:
    return f"http://{SETUP_HOST}:{cfg.port}"


def can_swap(cfg: ServerConfig, origin: str) -> bool:
    """The real app can take over the setup socket only when browsers reach it at the same origin."""
    return cfg.host == SETUP_HOST and cfg.public_origin == origin and bool(LOCAL_ORIGIN.fullmatch(origin))


class Swappable:
    """Serves one ASGI app, then another on the same socket, running the second's lifespan itself."""

    def __init__(self, app: Any):
        self.app = app
        self._lifespan: Any = None

    async def swap(self, app: FastAPI) -> None:
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self._lifespan = lifespan
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            return await self.app(scope, receive, send)
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._lifespan is not None:
                    await self._lifespan.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
                return


class DatabaseBody(BaseModel):
    url: str | None = None


class AdminBody(BaseModel):
    username: str
    password: str


@dataclass
class Setup:
    path: Path
    origin: str
    token: str | None
    finish: Callable[[ServerConfig], Awaitable[bool]]
    base_path: str
    static_dir: Path
    clock: Callable[[], datetime]
    admin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def config(self) -> ServerConfig:
        return load_config(self.path)

    def step(self) -> str:
        """``database`` until the config, schema, and master key are ready, then ``admin``."""
        if not self.path.exists():
            return "database"
        cfg = self.config()
        try:
            keyring_from_source(cfg.master_key)
        except (MasterKeyError, NotImplementedError):
            return "database"
        engine = make_engine(cfg.database_url)
        try:
            with engine.connect() as conn:
                return "admin" if inspect(conn).has_table("users") else "database"
        except SQLAlchemyError:
            return "database"
        finally:
            engine.dispose()

    def admin_exists(self) -> bool:
        if not self.path.exists():
            return False
        cfg = self.config()
        engine = make_engine(cfg.database_url)
        try:
            with engine.connect() as conn:
                return inspect(conn).has_table("users") and has_admin(conn, cfg.require_totp)
        except SQLAlchemyError:
            return False
        finally:
            engine.dispose()


def _setup(request: Request) -> Setup:
    return request.app.state.setup


# The peer must be loopback itself: no proxy header counts. The Host check stops a rebound DNS name.
def open_to(request: Request) -> Setup:
    setup = _setup(request)
    peer = request.client.host if request.client else ""
    if setup.token is None or peer not in LOOPBACK_PEERS or request.headers.get("host") != urlsplit(setup.origin).netloc:
        raise ApiError(404, "Not found.")
    if setup.admin_exists():
        setup.token = None
        raise ApiError(404, "Not found.")
    return setup


def with_token(request: Request, setup: Setup = Depends(open_to)) -> Setup:
    if not auth.safe_equal(request.headers.get(TOKEN_HEADER), setup.token or ""):
        raise ApiError(403, "This setup link is not valid. Use the link printed in the terminal.")
    if request.method != "GET" and request.headers.get("origin") != setup.origin:
        raise ApiError(403, "Origin not allowed.")
    return setup


def _refuse(exc: SetupError) -> None:
    raise ApiError(400, str(exc)) from exc


router = APIRouter()


@router.get("/setup")
def page(setup: Setup = Depends(open_to)) -> Response:
    meta = f'<meta name="armature-base" content="{html.escape(setup.base_path, quote=True)}">'.encode()
    body = BASE_META.sub(lambda _m: meta, (setup.static_dir / "setup.html").read_bytes(), count=1)
    return Response(body, media_type="text/html; charset=utf-8")


@router.get("/static/{name}")
def asset(name: str, setup: Setup = Depends(open_to)) -> Response:
    if name not in FILES:
        raise ApiError(404, "Not found.")
    path = setup.static_dir / name
    return Response(path.read_bytes(), media_type=TYPES[path.suffix])


@router.get("/setup/api/state")
def state(setup: Setup = Depends(with_token)) -> dict[str, Any]:
    exists = setup.path.exists()
    cfg = setup.config()
    key = key_file_of(cfg)
    return {"step": setup.step(), "config_file": str(setup.path), "config_exists": exists,
            "database_from_env": not exists and bool(cfg.model_fields_set & {"database_url"}),
            "key_file": str(key) if key else None, "password_min": PASSWORD_MIN, "totp": cfg.require_totp}


@router.post("/setup/api/database/test")
def test_database(body: DatabaseBody, setup: Setup = Depends(with_token)) -> dict[str, Any]:
    try:
        try_database(body.url or "")
    except SetupError as exc:
        _refuse(exc)
    return {"ok": True}


@router.post("/setup/api/database")
def database(body: DatabaseBody, setup: Setup = Depends(with_token)) -> dict[str, Any]:
    try:
        if body.url and not setup.path.exists():
            try_database(body.url)
            write_new_config(setup.path, database_url=check_database_url(body.url))
        prepared = prepare(setup.path)
    except SetupError as exc:
        _refuse(exc)
    except (MasterKeyError, NotImplementedError) as exc:
        raise ApiError(400, f"The master key can't be read: {exc}") from exc
    except SQLAlchemyError as exc:
        raise ApiError(400, f"The database could not be prepared ({exc.__class__.__name__}).") from exc
    key = key_file_of(prepared.cfg)
    return {"config_file": str(setup.path), "key_file": str(key) if key else None,
            "new_key": prepared.new_key is not None}


def _create(setup: Setup, cfg: ServerConfig, body: AdminBody, ip: str) -> str:
    keyring = keyring_from_source(cfg.master_key)
    engine = make_engine(cfg.database_url)
    try:
        with engine.begin() as conn:
            if has_admin(conn, cfg.require_totp):
                raise ApiError(409, "An admin already exists.")
            try:
                admin = create_admin(conn, keyring, username=body.username, password=body.password,
                                     require_totp=False)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            token, _ = auth.start_session(conn, keyring, admin.user_id, setup.clock(), ip=ip, step_up=True)
    finally:
        engine.dispose()
    return token


@router.post("/setup/api/admin")
async def admin(body: AdminBody, request: Request, setup: Setup = Depends(with_token)) -> JSONResponse:
    if await asyncio.to_thread(setup.step) != "admin":
        raise ApiError(409, "Set up the database first.")
    cfg = await asyncio.to_thread(setup.config)
    if cfg.require_totp:
        raise ApiError(409, "Authenticator codes are on in the config. Run `armature server create-admin` instead.")
    peer = request.client.host if request.client else ""
    async with setup.admin_lock:
        if setup.token is None:
            raise ApiError(404, "Not found.")
        token = await asyncio.to_thread(_create, setup, cfg, body, peer)
        setup.token = None
    if not await setup.finish(cfg):
        return JSONResponse({"next": None, "sign_in": f"{cfg.public_origin}{cfg.base_path}/login"})
    response = JSONResponse({"next": f"{cfg.base_path}/#/setup", "sign_in": None})
    srv = Server(cfg=cfg, engine=None, keyring=None, clock=setup.clock, dummy_hash="", static_dir=setup.static_dir)
    set_session_cookie(srv, response, token)
    return response


def create_setup_app(path: Path, *, origin: str, token: str, finish: Callable[[ServerConfig], Awaitable[bool]],
                     base_path: str = "", static_dir: Path = STATIC_DIR,
                     clock: Callable[[], datetime] = utcnow) -> FastAPI:
    app = FastAPI(title="Armature setup", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.setup = Setup(path=path, origin=origin, token=token, finish=finish, base_path=base_path,
                            static_dir=static_dir, clock=clock)
    install_error_handlers(app)
    app.include_router(router, prefix=base_path)
    app.add_middleware(SecurityHeaders, hsts=False)
    return app
