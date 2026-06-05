from __future__ import annotations
import asyncio
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from armature.service.models import RunRequest, RunResponse
from armature.service.registry import WorkflowRegistry
from armature.service.jobs import JobStore, JobStatus
from armature.spec.loader import load_spec
from armature.runtime.engine import Harness
from armature.hooks.lifecycle import HookPhase, HookDecision

_jobs = JobStore()


def build_app(registry: WorkflowRegistry | None = None) -> FastAPI:
    """Create and return a configured FastAPI application.

    Pass a pre-loaded WorkflowRegistry to enable /workflows routes.
    The default module-level `app` uses an empty registry (path-based
    /run endpoints remain available for backward compatibility).
    """
    _registry = registry or WorkflowRegistry()
    fastapi_app = FastAPI(title="Armature Service", version="0.1.0")

    # ── Health ────────────────────────────────────────────────────────────────

    @fastapi_app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    # ── Workflow registry routes ───────────────────────────────────────────────

    @fastapi_app.get("/workflows")
    async def list_workflows():
        """List all registered workflows."""
        return _registry.list_all()

    @fastapi_app.get("/workflows/{name}")
    async def get_workflow(name: str):
        """Return metadata for a named workflow."""
        spec = _registry.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        return {
            "name": spec.name,
            "description": spec.description,
            "version": spec.version,
            "stages": [{"id": s.id, "role": s.role.type.value if s.role else None}
                       for s in spec.stages],
        }

    @fastapi_app.post("/workflows/{name}/run", response_model=RunResponse)
    async def run_named_workflow(name: str, request: Request):
        """Run a registered workflow by name (synchronous)."""
        spec = _registry.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        body = await request.json()
        inputs = body.get("inputs", {})
        try:
            harness = Harness(spec=spec)
            result = await harness.run(inputs)
            return RunResponse(run_id=harness._run_id, status="complete", result=result)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fastapi_app.post("/workflows/{name}/run/async", status_code=202)
    async def run_named_workflow_async(name: str, request: Request):
        """Run a registered workflow by name (async — returns job_id)."""
        spec = _registry.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        body = await request.json()
        inputs = body.get("inputs", {})
        job = _jobs.create()
        asyncio.create_task(_execute_async_spec(job.job_id, spec, inputs))
        return {"job_id": job.job_id, "status": job.status}

    # ── Legacy path-based routes (backward compatibility) ─────────────────────

    @fastapi_app.post("/run", response_model=RunResponse)
    async def run_workflow(req: RunRequest):
        spec_path = Path(req.spec_path)
        if not spec_path.exists():
            raise HTTPException(status_code=404, detail=f"Spec not found: {req.spec_path}")
        try:
            session_dir = Path(req.session_dir) if req.session_dir else None
            spec = load_spec(spec_path, vars=req.inputs)
            harness = Harness(spec=spec, session_dir=session_dir)
            result = await harness.run(req.inputs)
            return RunResponse(run_id=harness._run_id, status="complete", result=result)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @fastapi_app.post("/run/async", status_code=202)
    async def run_workflow_async(req: RunRequest):
        spec_path = Path(req.spec_path)
        if not spec_path.exists():
            raise HTTPException(status_code=404, detail=f"Spec not found: {req.spec_path}")
        job = _jobs.create()
        asyncio.create_task(_execute_async(job.job_id, req))
        return {"job_id": job.job_id, "status": job.status}

    @fastapi_app.get("/run/{job_id}/events")
    async def get_job_events(job_id: str):
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

        async def event_stream():
            while True:
                event = await job.events.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "run_complete":
                    break

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @fastapi_app.get("/run/{job_id}")
    async def get_job(job_id: str):
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        return {
            "job_id": job.job_id,
            "status": job.status,
            "run_id": job.run_id,
            "result": job.result,
            "error": job.error,
        }

    return fastapi_app


async def _execute_async_spec(job_id: str, spec, inputs: dict) -> None:
    await _jobs.mark_running(job_id)
    try:
        harness = Harness(spec=spec)
        _attach_job_hooks(harness, job_id)
        result = await harness.run(inputs)
        await _jobs.mark_complete(job_id, harness._run_id, result)
    except Exception as exc:
        await _jobs.mark_failed(job_id, str(exc))


async def _execute_async(job_id: str, request: RunRequest) -> None:
    await _jobs.mark_running(job_id)
    try:
        session_dir = Path(request.session_dir) if request.session_dir else None
        spec = load_spec(Path(request.spec_path), vars=request.inputs)
        harness = Harness(spec=spec, session_dir=session_dir)
        _attach_job_hooks(harness, job_id)
        result = await harness.run(request.inputs)
        await _jobs.mark_complete(job_id, harness._run_id, result)
    except Exception as exc:
        await _jobs.mark_failed(job_id, str(exc))


def _attach_job_hooks(harness: Harness, job_id: str) -> None:
    async def pre_stage_hook(phase, stage_id, args, ctx) -> HookDecision:
        await _jobs.emit_stage_event(job_id, {"type": "stage_start", "stage_id": stage_id})
        return HookDecision.ALLOW

    async def post_stage_hook(phase, stage_id, result, ctx) -> None:
        spec_stage = next((s for s in harness._spec.stages if s.id == stage_id), None)
        if spec_stage and spec_stage.response_stage:
            content = result.get("content", "") if isinstance(result, dict) else ""
            await _jobs.emit_stage_event(job_id, {
                "type": "response_stage_complete",
                "stage_id": stage_id,
                "content": content,
            })
        await _jobs.emit_stage_event(job_id, {"type": "stage_complete", "stage_id": stage_id})

    async def on_token(chunk: str) -> None:
        await _jobs.emit_stage_event(job_id, {"type": "token", "content": chunk})

    harness._hooks.register(HookPhase.PRE_STAGE, pre_stage_hook)
    harness._hooks.register(HookPhase.POST_STAGE, post_stage_hook)
    harness._on_token = on_token


# Default singleton — used by `armature serve` when no --specs-dir is given
app = build_app()
