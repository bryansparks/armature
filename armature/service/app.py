from __future__ import annotations
import asyncio
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from armature.service.models import RunRequest, RunResponse
from armature.service.jobs import JobStore, JobStatus
from armature.spec.loader import load_spec
from armature.runtime.engine import Harness
from armature.hooks.lifecycle import HookPhase, HookDecision

app = FastAPI(title="Armature Service", version="0.1.0")

_jobs = JobStore()


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/run", response_model=RunResponse)
async def run_workflow(request: RunRequest):
    spec_path = Path(request.spec_path)
    if not spec_path.exists():
        raise HTTPException(status_code=404, detail=f"Spec not found: {request.spec_path}")

    try:
        session_dir = Path(request.session_dir) if request.session_dir else None
        spec = load_spec(spec_path, vars=request.inputs)
        harness = Harness(spec=spec, session_dir=session_dir)
        result = await harness.run(request.inputs)
        return RunResponse(run_id=harness._run_id, status="complete", result=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/run/async", status_code=202)
async def run_workflow_async(request: RunRequest):
    spec_path = Path(request.spec_path)
    if not spec_path.exists():
        raise HTTPException(status_code=404, detail=f"Spec not found: {request.spec_path}")

    job = _jobs.create()
    asyncio.create_task(_execute_async(job.job_id, request))
    return {"job_id": job.job_id, "status": job.status}


@app.get("/run/{job_id}/events")
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


@app.get("/run/{job_id}")
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
        await _jobs.emit_stage_event(job_id, {"type": "stage_complete", "stage_id": stage_id})

    harness._hooks.register(HookPhase.PRE_STAGE, pre_stage_hook)
    harness._hooks.register(HookPhase.POST_STAGE, post_stage_hook)
