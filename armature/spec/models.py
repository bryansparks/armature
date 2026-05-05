from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class RoleType(str, Enum):
    WORKER = "worker"
    ORCHESTRATOR = "orchestrator"
    JUDGE = "judge"
    RESEARCHER = "researcher"


class OutputMode(str, Enum):
    TEXT = "text"
    GUIDED_JSON = "guided_json"
    JSON = "json"


class ModelTierConfig(BaseModel):
    provider: str
    model: str
    api_base: str | None = None

# Alias for convenience
ModelTier = ModelTierConfig


class ModelTiers(BaseModel):
    tiny: ModelTierConfig | None = None
    small: ModelTierConfig | None = None
    medium: ModelTierConfig | None = None
    large: ModelTierConfig | None = None
    frontier: ModelTierConfig | None = None


class Contract(BaseModel):
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    completion: str | None = None
    max_iterations: int = 20
    max_llm_calls: int = 100
    timeout_hours: float = 8.0


class Signature(BaseModel):
    input: dict[str, str] = Field(default_factory=dict)
    output: dict[str, str] = Field(default_factory=dict)


class Role(BaseModel):
    name: str
    type: RoleType
    description: str
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    model_tier: str = "small"


class LoopConfig(BaseModel):
    stage: str
    context: str = "retry"
    max: int = 3
    until: str | None = None


class OnFailConfig(BaseModel):
    loop: LoopConfig | None = None


class Adapter(BaseModel):
    name: str
    type: str  # "python" | "script"
    fn: str | None = None
    cmd: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class Failure(BaseModel):
    condition: str
    recovery: str
    max_retries: int = 3


class SafetyCondition(BaseModel):
    field: str
    op: Literal["contains", "not_contains", "equals", "not_equals", "matches_regex", "truthy"]
    value: str = ""


class ToolSafetyRule(BaseModel):
    tool: str
    condition: SafetyCondition
    action: Literal["block", "warn", "log"]
    message: str = ""


class FileState(BaseModel):
    enabled: bool = False
    base: str = "~/.armature/runs/{{run_id}}/"
    workspace: str = "workspace/"
    manifest: str = "manifest.json"


class Stage(BaseModel):
    id: str
    role: Role | None = None
    depends_on: list[str] = Field(default_factory=list)
    adapter: str | None = None
    gate: str | None = None
    signature: Signature | None = None
    output_mode: OutputMode = OutputMode.TEXT
    on_fail: OnFailConfig | None = None
    present: str | None = None
    condition: str | None = None
    output_schema: dict[str, Any] | None = None   # JSON Schema for GUIDED_JSON output
    subagent_spec: str | None = None              # Path to child workflow spec file
    fan_out: int | None = None
    fan_in: Literal["list", "merge", "first"] = "list"
    partition_key: str | None = None


class TraceConfig(BaseModel):
    enabled: bool = True
    metrics: list[str] = Field(default_factory=list)
    filesystem: str = "~/.armature/traces/{{run_id}}/"


class HarnessSpec(BaseModel):
    name: str
    version: str = "1.0"
    description: str = ""
    contracts: Contract = Field(default_factory=Contract)
    roles: dict[str, Role] = Field(default_factory=dict)
    stages: list[Stage]
    adapters: dict[str, Adapter] = Field(default_factory=dict)
    failures: dict[str, Failure] = Field(default_factory=dict)
    model_tiers: ModelTiers = Field(default_factory=ModelTiers)
    file_state: FileState = Field(default_factory=FileState)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    safety_rules: list[ToolSafetyRule] = Field(default_factory=list)
