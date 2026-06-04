"""Tests for response_stage token and response_stage_complete SSE events."""
import asyncio
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed; install with pip install armature[service]")

from unittest.mock import MagicMock
from armature.service.app import _attach_job_hooks, _jobs
from armature.hooks.lifecycle import HookRegistry
from armature.spec.models import Stage, Role, RoleType


def _make_mock_harness(stages: list) -> MagicMock:
    harness = MagicMock()
    harness._hooks = HookRegistry()
    harness._spec.stages = stages
    return harness


def _make_response_stage() -> Stage:
    return Stage(
        id="respond",
        role=Role(name="r", type=RoleType.WORKER, description="answer"),
        depends_on=[],
        response_stage=True,
    )


def _make_normal_stage() -> Stage:
    return Stage(
        id="process",
        role=Role(name="r", type=RoleType.WORKER, description="work"),
        depends_on=[],
        response_stage=False,
    )


async def test_token_events_appear_in_sse_stream():
    """_attach_job_hooks sets harness._on_token; calling it emits token events to the SSE queue."""
    job = _jobs.create()
    harness = _make_mock_harness([_make_response_stage()])
    _attach_job_hooks(harness, job.job_id)

    assert callable(harness._on_token)

    await harness._on_token("Hello")
    await harness._on_token(", world")

    event1 = await asyncio.wait_for(job.events.get(), timeout=1.0)
    event2 = await asyncio.wait_for(job.events.get(), timeout=1.0)

    assert event1 == {"type": "token", "content": "Hello"}
    assert event2 == {"type": "token", "content": ", world"}


async def test_response_stage_complete_event_emitted():
    """post_stage_hook emits response_stage_complete before stage_complete for response stages."""
    job = _jobs.create()
    harness = _make_mock_harness([_make_response_stage(), _make_normal_stage()])
    _attach_job_hooks(harness, job.job_id)

    # Simulate pre_stage_hook (emits stage_start) then post_stage_hook for respond
    await harness._hooks.run_pre_stage("respond", {})
    await harness._hooks.run_post_stage("respond", {"content": "Sure, here it is."}, {})

    events = []
    for _ in range(3):  # stage_start + response_stage_complete + stage_complete
        events.append(await asyncio.wait_for(job.events.get(), timeout=1.0))

    types = [e["type"] for e in events]
    assert "response_stage_complete" in types
    assert types.index("response_stage_complete") < types.index("stage_complete")

    rsc = next(e for e in events if e["type"] == "response_stage_complete")
    assert rsc["stage_id"] == "respond"
    assert rsc["content"] == "Sure, here it is."
