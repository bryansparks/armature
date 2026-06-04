from __future__ import annotations
import asyncio
import time
from typing import Awaitable, Callable

from croniter import croniter

from armature.spec.models import CronTrigger, WebhookTrigger


class TriggerDispatcher:
    async def run_forever(
        self,
        spec: "armature.spec.models.HarnessSpec",
        run_fn: Callable[[dict], Awaitable[None]],
        host: str = "0.0.0.0",
        port: int = 8081,
    ) -> None:
        """Start all trigger listeners; block until cancelled."""
        tasks = []
        cron_triggers = [t for t in spec.triggers if isinstance(t, CronTrigger)]
        webhook_triggers = [t for t in spec.triggers if isinstance(t, WebhookTrigger)]

        for trigger in cron_triggers:
            tasks.append(asyncio.create_task(self._cron_loop(trigger, run_fn)))

        if webhook_triggers:
            tasks.append(asyncio.create_task(
                self._webhook_server(webhook_triggers, run_fn, host, port)
            ))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise

    async def _cron_loop(
        self,
        trigger: CronTrigger,
        run_fn: Callable[[dict], Awaitable[None]],
    ) -> None:
        while True:
            next_fire = croniter(trigger.schedule, time.time()).get_next(float)
            delay = max(0.0, next_fire - time.time())
            await asyncio.sleep(delay)
            await run_fn({})

    async def _webhook_server(
        self,
        triggers: list[WebhookTrigger],
        run_fn: Callable[[dict], Awaitable[None]],
        host: str,
        port: int,
    ) -> None:
        import uvicorn
        app = self._build_webhook_app(triggers, run_fn)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()

    def _build_webhook_app(
        self,
        triggers: list[WebhookTrigger],
        run_fn: Callable[[dict], Awaitable[None]],
    ):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        def _make_route(p: str):
            async def endpoint(request: Request) -> JSONResponse:
                body = await request.json()
                await run_fn({"body": body, "path": p})
                return JSONResponse({"status": "ok"})
            endpoint.__name__ = f"webhook_{p.replace('/', '_').strip('_')}"
            return Route(p, endpoint=endpoint, methods=["POST"])

        routes = [_make_route(t.path) for t in triggers]
        return Starlette(routes=routes)
