from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, HTTPException
from armature.service.models import RunRequest, RunResponse
from armature.spec.loader import load_spec
from armature.runtime.engine import Harness

app = FastAPI(title="Armature Service", version="0.1.0")


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
