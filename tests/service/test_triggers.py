"""Tests for TriggerDispatcher: cron scheduling and webhook routing."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from armature.spec.models import CronTrigger, WebhookTrigger
from armature.service.triggers import TriggerDispatcher


async def test_cron_next_fire_computed_correctly():
    """croniter returns a next-fire time in the future for a standard expression."""
    from croniter import croniter
    import time
    expr = "0 9 * * *"
    nxt = croniter(expr, time.time()).get_next(float)
    assert nxt > time.time()


async def test_cron_loop_calls_run_fn_at_schedule():
    """_cron_loop fires run_fn once when the scheduled time arrives."""
    trigger = CronTrigger(schedule="* * * * *")
    run_fn = AsyncMock()
    dispatcher = TriggerDispatcher()
    sleep_calls = 0

    async def fake_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError()

    with patch("armature.service.triggers.asyncio.sleep", side_effect=fake_sleep):
        try:
            await dispatcher._cron_loop(trigger, run_fn)
        except asyncio.CancelledError:
            pass

    run_fn.assert_called_once()


async def test_webhook_route_registered_for_each_path():
    """A FastAPI app built for webhooks exposes one POST route per path."""
    triggers = [
        WebhookTrigger(path="/webhook/wf1"),
        WebhookTrigger(path="/webhook/wf2"),
    ]
    run_fn = AsyncMock()
    dispatcher = TriggerDispatcher()
    app = dispatcher._build_webhook_app(triggers, run_fn)

    routes = {r.path for r in app.routes}
    assert "/webhook/wf1" in routes
    assert "/webhook/wf2" in routes


async def test_webhook_post_calls_run_fn_with_payload():
    """POSTing to a webhook route calls run_fn with body and path."""
    from httpx import AsyncClient, ASGITransport
    triggers = [WebhookTrigger(path="/webhook/test")]
    calls = []

    async def capturing_run_fn(payload):
        calls.append(payload)

    dispatcher = TriggerDispatcher()
    app = dispatcher._build_webhook_app(triggers, capturing_run_fn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/webhook/test", json={"event": "tick"})

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["body"] == {"event": "tick"}
    assert calls[0]["path"] == "/webhook/test"
