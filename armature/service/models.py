from __future__ import annotations
from typing import Any
from pydantic import BaseModel


class RunRequest(BaseModel):
    spec_path: str
    inputs: dict[str, Any] = {}
    session_dir: str | None = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
