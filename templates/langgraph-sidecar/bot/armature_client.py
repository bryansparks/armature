"""Client for calling Armature as an async sidecar service."""
from __future__ import annotations
import asyncio
import json
import os
import httpx

ARMATURE_URL = os.environ.get("ARMATURE_URL", "http://localhost:8100")
_POLL_INTERVAL = 0.5   # seconds between status polls
_TIMEOUT = 120.0       # max seconds to wait for workflow completion


async def run_workflow(spec_path: str, inputs: dict) -> dict:
    """Submit a workflow and poll until complete. Returns the result dict."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{ARMATURE_URL}/run/async",
            json={"spec_path": spec_path, "inputs": inputs},
        )
        resp.raise_for_status()
        job_id = resp.json()["job_id"]

    loop = asyncio.get_event_loop()
    deadline = loop.time() + _TIMEOUT
    async with httpx.AsyncClient(timeout=30) as client:
        while loop.time() < deadline:
            status_resp = await client.get(f"{ARMATURE_URL}/run/{job_id}")
            status_resp.raise_for_status()
            body = status_resp.json()
            if body["status"] == "complete":
                return body["result"]
            if body["status"] == "failed":
                raise RuntimeError(f"Armature workflow failed: {body.get('error')}")
            await asyncio.sleep(_POLL_INTERVAL)

    raise TimeoutError(f"Armature workflow did not complete within {_TIMEOUT}s")


async def stream_workflow_events(spec_path: str, inputs: dict):
    """Submit a workflow and yield SSE events as they arrive.

    Yields dicts like:
        {"event": "stage_start", "stage_id": "gather"}
        {"event": "stage_complete", "stage_id": "gather", "latency_ms": 1240}
        {"event": "run_complete", "run_id": "...", "result": {...}}

    Use this when you want to forward progress updates to the user.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{ARMATURE_URL}/run/async",
            json={"spec_path": spec_path, "inputs": inputs},
        )
        resp.raise_for_status()
        job_id = resp.json()["job_id"]

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("GET", f"{ARMATURE_URL}/run/{job_id}/events") as stream:
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])
