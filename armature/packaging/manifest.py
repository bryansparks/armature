from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

PACKAGE_API_VERSION = "armature.package/v1"


class SecretRequirement(BaseModel):
    name: str
    description: str | None = None
    providers: list[str] = Field(default_factory=list)


class SecretsFile(BaseModel):
    required: list[SecretRequirement] = Field(default_factory=list)


class ArtifactSpec(BaseModel):
    stage_id: str
    name: str
    format: Literal["markdown", "json", "text"] = "text"


class Destinations(BaseModel):
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    include_trace: bool = False
    results_layout: Literal["by_run_id"] = "by_run_id"


class PackageManifest(BaseModel):
    api_version: str = PACKAGE_API_VERSION
    name: str
    version: str
    spec: str = "workflow.yaml"
    inputs: str = "inputs.yaml"
    requirements: str | None = "requirements.txt"
    requirements_lock: str | None = "requirements.lock"
    tools_dir: str | None = "tools/"
    secrets: str = "secrets.yaml"
    destinations: str = "destinations.yaml"
    runtime_inputs: list[str] = Field(default_factory=list)
    armature_version: str
    created_at: str
    created_by: str = "armature package build"
    integrity: str = "manifest.sha256"


class ArtifactResult(BaseModel):
    name: str
    stage_id: str
    format: str
    path: str


class TraceRef(BaseModel):
    included: bool
    path: str | None = None


class ResultsManifest(BaseModel):
    """The run receipt — written to results/<run_id>/receipt.json."""
    package_name: str
    package_version: str
    run_id: str
    status: Literal["complete", "failed"]
    started_at: str
    finished_at: str
    duration_s: float
    exit_code: int
    armature_version: str
    artifacts: list[ArtifactResult] = Field(default_factory=list)
    trace: TraceRef
    error: str | None = None