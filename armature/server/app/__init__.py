"""The Armature server's FastAPI app: a signed-in JSON API and the browser UI."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine

from armature.server import actions, worker
from armature.server.app import (
    routes_actions,
    routes_api,
    routes_assist,
    routes_auth,
    routes_defs,
    routes_dryrun,
    routes_export,
    routes_jobs,
    routes_live,
    routes_oidc,
    routes_packs,
    routes_runner,
    routes_stats,
    routes_status,
    routes_subjects,
    routes_webhooks,
    static,
)
from armature.server.app.deps import Server
from armature.server.app.http import BodyLimit, SecurityHeaders, install_error_handlers
from armature.server.auth import dummy_hash
from armature.server.config import ServerConfig
from armature.server.crypto import Keyring
from armature.server.db import utcnow
from armature.server.export import ExportHook
from armature.server.ingest import LiveHub
from armature.server.oidc import Provider
from armature.server.status import StatusHook

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(cfg: ServerConfig, engine: Engine, keyring: Keyring, *, clock: Callable[[], datetime] = utcnow,
               static_dir: Path = STATIC_DIR, oidc_transport: httpx.BaseTransport | None = None,
               password_cost: int | None = None, worker_interval: float | None = worker.INTERVAL_S) -> FastAPI:
    hub = LiveHub(engine, keyring, clock, socket_path=cfg.events_socket,
                  lost_after=timedelta(minutes=cfg.lost_after_minutes), sse_per_user=cfg.sse_per_user,
                  sse_total=cfg.sse_total)

    export = ExportHook(engine, cfg.export_on_change, clock=clock) if cfg.export_on_change else None
    if export is not None:
        export.attach()
    status = StatusHook(engine, keyring, cfg.status, clock=clock) if cfg.status else None
    job_worker = worker.Worker(engine, keyring, cfg, hub, clock=clock, interval=worker_interval or worker.INTERVAL_S)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await hub.start()
        if worker_interval is not None:
            await asyncio.to_thread(job_worker.start)
        if export is not None:
            export.schedule()
        if cfg.actions:
            await asyncio.to_thread(actions.recover, engine, clock())
        if status is not None:
            status.start()
        try:
            yield
        finally:
            await asyncio.to_thread(job_worker.stop)
            await hub.stop()
            if export is not None:
                await asyncio.to_thread(export.stop)
            if status is not None:
                await asyncio.to_thread(status.stop)

    app = FastAPI(title="Armature", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.live = hub
    app.state.export = export
    app.state.status = status
    app.state.worker = job_worker
    app.state.server = Server(
        cfg=cfg, engine=engine, keyring=keyring, clock=clock, dummy_hash=dummy_hash(password_cost),
        static_dir=static_dir, provider=Provider(cfg.oidc, oidc_transport) if cfg.oidc else None,
        password_cost=password_cost,
    )
    install_error_handlers(app)
    base = cfg.base_path

    health = APIRouter()

    @health.get("/health")
    def get_health() -> dict[str, bool]:
        return {"ok": True}

    for router in (health, routes_auth.router, routes_api.router, routes_defs.router, routes_live.router,
                   routes_runner.router, routes_oidc.router, routes_packs.router, routes_export.router,
                   routes_assist.router, routes_dryrun.router, routes_stats.router, routes_subjects.router,
                   routes_actions.router, routes_status.router, routes_webhooks.router, routes_jobs.router,
                   static.router):
        app.include_router(router, prefix=base)

    if base:
        @app.get(base, include_in_schema=False)
        def base_redirect() -> RedirectResponse:
            return RedirectResponse(f"{base}/", status_code=302)

    overrides = {f"{base}{path}": routes_defs.DEFS_BODY_LIMIT for path in routes_defs.DEFS_PATHS}
    overrides[f"{base}{routes_assist.ASSIST_PATH}"] = routes_assist.ASSIST_BODY_LIMIT
    overrides[f"{base}{routes_webhooks.HOOKS_PATH}"] = routes_webhooks.HOOKS_BODY_LIMIT
    app.add_middleware(BodyLimit, overrides={**overrides, f"{base}/api/events": routes_runner.EVENTS_BODY_LIMIT})
    app.add_middleware(SecurityHeaders, hsts=not app.state.server.insecure_localhost)
    return app
