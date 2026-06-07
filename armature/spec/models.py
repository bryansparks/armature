from __future__ import annotations
from enum import Enum
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    api_key_env: str | None = None    # env var name holding the API key for this tier
    temperature: float | None = None  # default temperature for calls on this tier
    max_tokens: int | None = None     # default max output tokens for this tier
    tool_calling: bool | None = None  # None → auto-detect by provider; True/False → explicit override

# Alias for convenience
ModelTier = ModelTierConfig


class ModelTiers(BaseModel):
    model_config = ConfigDict(extra="allow")

    tiny: ModelTierConfig | None = None
    small: ModelTierConfig | None = None
    medium: ModelTierConfig | None = None
    large: ModelTierConfig | None = None
    frontier: ModelTierConfig | None = None

    @model_validator(mode="after")
    def _coerce_extra_tiers(self) -> "ModelTiers":
        """Coerce extra tier entries (e.g. 'synthesis') from raw dicts/CommentedMaps."""
        if self.__pydantic_extra__:
            for key, val in list(self.__pydantic_extra__.items()):
                if val is not None and not isinstance(val, ModelTierConfig):
                    self.__pydantic_extra__[key] = ModelTierConfig.model_validate(dict(val))
        return self


class RoleTypeDefaults(BaseModel):
    """Maps each role type to the tier it uses when model_tier is not set on the role.

    These defaults encode the intent: workers are cheap/fast, judges and
    orchestrators need the best reasoning, researchers need strong synthesis.
    Override in the spec's role_type_defaults section.
    """
    worker: str = "small"
    orchestrator: str = "frontier"
    judge: str = "frontier"
    researcher: str = "large"


class Contract(BaseModel):
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    completion: str | None = None
    max_iterations: int = 20
    max_llm_calls: int = 100
    timeout_hours: float = 8.0
    output_max_chars: int | None = None  # default per-stage output truncation limit


class Signature(BaseModel):
    input: dict[str, str] = Field(default_factory=dict)
    output: dict[str, str] = Field(default_factory=dict)


class Role(BaseModel):
    name: str
    type: RoleType
    description: str
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    model_tier: str | None = None      # None → resolved from role_type_defaults
    temperature: float | None = None   # overrides tier-level temperature
    max_tokens: int | None = None      # overrides tier-level max_tokens


class LoopConfig(BaseModel):
    stage: str
    context: str = "retry"
    max: int = 3
    until: str | None = None
    backoff_s: float | None = None   # initial wait before retry 1; doubles each subsequent attempt
    backoff_max_s: float = 60.0      # cap on per-attempt wait time


class OnFailConfig(BaseModel):
    loop: LoopConfig | None = None


class Adapter(BaseModel):
    name: str
    type: str  # "python" | "script"
    fn: str | None = None
    cmd: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    timeout: int = 60


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
    action: Literal["block", "warn", "log", "require_approval", "allow"]
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
    tool_call: ToolCallConfig | None = None
    signature: Signature | None = None
    output_mode: OutputMode = OutputMode.TEXT
    on_fail: OnFailConfig | None = None
    present: str | None = None
    condition: str | None = None
    skip_if: str | None = None                    # Jinja2 expr; stage skipped when it renders truthy
    timeout_s: float | None = None                # wall-clock limit for the whole stage (incl. retries)
    fail_as_value: bool = False                   # on failure, return {"_failed": True, ...} instead of raising
    output_schema: dict[str, Any] | None = None   # JSON Schema for GUIDED_JSON output
    subagent_spec: str | None = None              # Path to child workflow spec file
    isolated: bool = False             # when True, strips parent context to signature.input keys only
    fan_out: int | None = None          # max parallelism; if set, stage fans out over partition_source
    fan_in: Literal["list", "merge", "first", "consensus"] = "list"
    partition_key: str | None = None    # context variable name for each partition item
    partition_source: str | None = None # Jinja2 expression resolving to a list of items
    inject_file_as: str | None = None   # if set, read each item as a file path and inject content under this key
    output_max_chars: int | None = None # per-stage override; truncates stored result; falls back to contracts.output_max_chars
    evaluate: list[str] = Field(default_factory=list)  # declarative quality criteria evaluated post-run
    post_run: bool = False             # when True, stage runs after all normal stages with full transcript + diagnostics
    response_stage: bool = False       # when True, stream tokens to caller in real time
    sandbox_image: str | None = None   # per-stage Docker image override; falls back to sandbox.image


class TraceConfig(BaseModel):
    enabled: bool = True
    metrics: list[str] = Field(default_factory=list)
    filesystem: str = "~/.armature/traces/{{run_id}}/"


class MemoryCapture(BaseModel):
    stage: str           # stage id whose output to capture
    key: str             # output key to persist
    max_entries: int = 5 # rolling window size — oldest entry evicted first


class MemoryConfig(BaseModel):
    enabled: bool = True
    fresh: bool = False               # when True, skip loading prior memories (start each run clean)
    capture: list[MemoryCapture] = Field(default_factory=list)
    inject_as: str = "_memory"        # context key injected at run start
    db: str | None = None             # override db path; defaults to ~/.armature/memory/{name}.db
    extract_knowledge: bool = False   # run KnowledgeExtractor post-run to build long-term facts
    inject_knowledge_as: str = "_knowledge"  # context key for injected knowledge facts


class ToolModule(BaseModel):
    module: str  # dotted Python import path; must expose register(registry) -> None


class ToolCallConfig(BaseModel):
    name: str                                    # registered tool name to invoke
    args: dict[str, Any] = Field(default_factory=dict)  # args; string values are Jinja2-rendered against context


class SkillDef(BaseModel):
    id: str
    description: str
    content: str | None = None   # inline skill text
    path: str | None = None      # path to a file containing the skill text

    def model_post_init(self, __context: Any) -> None:
        if self.content is None and self.path is None:
            raise ValueError(f"SkillDef '{self.id}' must have either 'content' or 'path'")


class MCPServerConfig(BaseModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None          # stdio only
    args: list[str] = Field(default_factory=list)
    url: str | None = None              # http/sse only
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 30.0


class SandboxMode(str, Enum):
    NONE = "none"
    DOCKER = "docker"


class SandboxConfig(BaseModel):
    mode: SandboxMode = SandboxMode.NONE
    image: str = "python:3.11-slim"
    timeout_s: float = 300.0
    allow_network: bool = False
    workspace: str = "/workspace"
    host_workspace: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    cpu_limit: str | None = None    # e.g. "1.5" → --cpus 1.5
    memory_limit: str | None = None # e.g. "512m" → --memory 512m
    runtime: str = "docker"         # container CLI binary: "docker", "podman", "nerdctl"
    platform: str | None = None     # e.g. "linux/amd64" → --platform linux/amd64


class ContinuationKey(BaseModel):
    key: str  # "stage_id.output_key" dotted notation


class ContinuationConfig(BaseModel):
    carry_forward: list[ContinuationKey] = Field(default_factory=list)
    inject_as: str = "prior_run"


class CronTrigger(BaseModel):
    type: Literal["cron"] = "cron"
    schedule: str


class WebhookTrigger(BaseModel):
    type: Literal["webhook"] = "webhook"
    path: str


TriggerConfig = Annotated[
    CronTrigger | WebhookTrigger,
    Field(discriminator="type"),
]


class HarnessSpec(BaseModel):
    name: str
    version: str = "1.0"
    description: str = ""
    mission: str = ""
    checkpoint: bool = False             # persist completed stage results for resume across runs
    contracts: Contract = Field(default_factory=Contract)
    roles: dict[str, Role] = Field(default_factory=dict)
    stages: list[Stage]
    adapters: dict[str, Adapter] = Field(default_factory=dict)
    failures: dict[str, Failure] = Field(default_factory=dict)
    model_tiers: ModelTiers = Field(default_factory=ModelTiers)
    role_type_defaults: RoleTypeDefaults = Field(default_factory=RoleTypeDefaults)
    file_state: FileState = Field(default_factory=FileState)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    safety_rules: list[ToolSafetyRule] = Field(default_factory=list)
    safety_mode: Literal["permissive", "strict"] = "permissive"
    memory: MemoryConfig | None = None
    continuation: ContinuationConfig | None = None
    triggers: list[TriggerConfig] = Field(default_factory=list)
    tools: list[ToolModule] = Field(default_factory=list)
    skill_library: dict[str, SkillDef] = Field(default_factory=dict)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
