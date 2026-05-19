# Plan: DeerFlow-Inspired Feature Set

**Status:** Design — pending implementation  
**Source:** DeerFlow architectural analysis (bytedance/deer-flow)  
**Philosophy:** Each feature is implemented the "Armature way" — declarative YAML, hook-based extensibility, no framework dependency.

---

## Feature Summary

| # | Feature | Complexity | Priority |
|---|---------|-----------|---------|
| a | MCP server support | Medium | High |
| b | Skills system (YAML-injectable context) | Low | High |
| c | `armature doctor` health check CLI | Low | Medium |
| d | LangFuse / LangSmith observability adapters | Low | Medium |
| e | Sandbox isolation for tool execution | High | Medium |
| f | Sub-agent context isolation flag | Low | High |
| g | Messaging channel connectors | High | Low |

---

## a) MCP Server Support

### Problem
Armature's tool registry requires Python modules to register tools. The MCP ecosystem (filesystem, search, databases, APIs) ships as ready-made tool servers — forcing Armature users to write Python wrappers for every external tool is unnecessary friction.

### The Armature Way
Declare MCP servers in the spec. Armature auto-discovers and registers their tools at harness init time. Tools appear in the registry under `{server_name}.{tool_name}` — matching the existing naming convention (`quorum.deliberate`, `tessera.retrieve`).

### New YAML

```yaml
mcp_servers:
  - name: filesystem
    transport: stdio
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /tmp/workspace]

  - name: search
    transport: http
    url: http://localhost:8001/mcp
    headers:
      Authorization: Bearer ${SEARCH_API_KEY}

  - name: postgres
    transport: stdio
    command: uvx
    args: [mcp-server-postgres, postgresql://localhost/mydb]
```

Stage usage (no change to stage model):
```yaml
- id: researcher
  role:
    name: Researcher
    type: researcher
    tools: [filesystem.read_file, search.search, web_search]  # MCP + built-in mixed
```

### Implementation Plan

**New spec fields** (`HarnessSpec`):
```python
class MCPServerConfig(BaseModel):
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None          # stdio only
    args: list[str] = []                # stdio only
    url: str | None = None              # http/sse only
    headers: dict[str, str] = {}        # http/sse only
    env: dict[str, str] = {}
    timeout_s: float = 30.0

class HarnessSpec(BaseModel):
    ...
    mcp_servers: list[MCPServerConfig] = []
```

**New module:** `armature/mcp/client.py`
- `MCPRegistrar.register_all(servers, registry)` — async, called from `Harness.__init__`
- Connects to each server via the `mcp` Python SDK
- Discovers available tools via `list_tools()`
- Wraps each as a `ToolDescriptor` with `handler = mcp_tool_caller(session, tool_name)`
- Registers as `{server_name}.{tool_name}`

**New optional dep:**
```toml
mcp = ["mcp>=1.0"]
```

**Engine change** (`Harness.__init__`):
```python
if self._spec.mcp_servers:
    from armature.mcp.client import MCPRegistrar
    self._mcp_sessions = await MCPRegistrar.register_all(
        self._spec.mcp_servers, self._registry
    )
```

MCP sessions are kept alive for the harness lifetime and cleaned up on `Harness.close()`.

---

## b) Skills System

### Problem
`Role.skills: list[str]` already exists in the spec model but is unwired — it does nothing. DeerFlow's insight: skills are reusable behavioral instructions (research protocols, output formats, domain conventions) that get injected into a stage's system prompt on demand. Only the stages that need a skill pay the context cost.

### The Armature Way
A `skill_library` at the spec level defines skills by ID. Stages reference them by ID in `role.skills`. `PromptAssembler` injects skill content into the system prompt at build time — after the role preamble, before the role description ends. Skills loaded from disk are resolved once at harness init.

### New YAML

```yaml
# Top-level skill_library (new spec section)
skill_library:
  - id: research_protocol
    description: Structured research and source verification protocol
    path: skills/research-protocol.md      # file path (relative to spec file)

  - id: citation_format
    description: How to format citations and references
    content: |                              # OR inline content
      Always cite sources as [Author, Year].
      Include the URL when available.
      Mark uncertain facts with [unverified].

  - id: json_discipline
    description: Strict JSON output discipline
    content: |
      Output valid JSON only. No markdown fences. No prose before or after.
      Use snake_case keys. Arrays over null values.

# Stage usage (Role.skills already exists — just needs wiring)
stages:
  - id: researcher
    role:
      name: Researcher
      type: researcher
      skills: [research_protocol, citation_format]   # ← injected into system prompt
      tools: [web_search, file_read]
```

### Implementation Plan

**New spec models:**
```python
class SkillDef(BaseModel):
    id: str
    description: str = ""
    path: str | None = None     # relative to spec file directory
    content: str | None = None  # inline content (mutually exclusive with path)

class HarnessSpec(BaseModel):
    ...
    skill_library: list[SkillDef] = []
```

**Spec loader change** (`loader.py`): after loading, resolve `path` entries relative to the spec file directory and populate `content`.

**PromptAssembler change** (`prompt.py`): when assembling the system prompt for a stage, look up each `role.skills` ID in the spec's skill library and append the content block:

```
[Role preamble]
[Role description]

---
## Skill: Research Protocol
[skill content here]

---
## Skill: Citation Format
[skill content here]
```

**Result:** Skills are free-form Markdown instruction blocks. They compose cleanly because each has its own labeled section. A stage with no skills gets no extra context — zero overhead.

---

## c) `armature doctor`

### Problem
New users often hit silent config failures: missing API keys, wrong model names, unwritable directories. There's no single command to verify the environment before running a workflow.

### The Armature Way
A `armature doctor` CLI subcommand that checks the environment, reports ✓/✗ for each item, and prints actionable fix hints. Optionally validates a specific spec file and runs a connectivity test against configured models.

### Usage

```bash
armature doctor                          # environment only
armature doctor my_workflow.yml          # + spec validation
armature doctor my_workflow.yml --full   # + model connectivity test (1-token call)
```

### Checks

| Category | Check | Fix hint |
|----------|-------|----------|
| Install | Python ≥ 3.11 | upgrade Python |
| Install | `aiosqlite`, `litellm`, `pydantic`, `jinja2`, `ruamel.yaml` importable | `pip install armature` |
| Optional | `sentence-transformers` importable | `pip install 'armature[embeddings]'` |
| Optional | `langfuse` importable | `pip install 'armature[langfuse]'` |
| Optional | `mcp` importable | `pip install 'armature[mcp]'` |
| Storage | `~/.armature/` directory writable | `mkdir -p ~/.armature && chmod 755 ~/.armature` |
| API keys | `ANTHROPIC_API_KEY` set (if anthropic tier configured) | `export ANTHROPIC_API_KEY=sk-ant-...` |
| API keys | `OPENAI_API_KEY` set (if openai tier configured) | same pattern |
| Spec | YAML parses without error | show parse error location |
| Spec | All `depends_on` stage IDs exist | list missing IDs |
| Spec | No cycles in DAG | show cycle |
| Connectivity | Quick 1-token call to each configured model tier | show HTTP error |

### Implementation Plan

New CLI command in `cli.py`: `@app.command("doctor")`
- Runs each check as a function returning `(ok: bool, message: str, fix: str | None)`
- Prints colored output (green ✓ / red ✗)
- Exits 0 if all pass, 1 if any fail (enables CI integration)

---

## d) LangFuse / LangSmith Observability Adapters

### Problem
LangFuse and LangSmith are the dominant LLM observability platforms. Armature has OpenTelemetry but not these, which limits adoption.

### The Armature Way
Adapters use the existing `HookRegistry`. They auto-activate from environment variables (zero spec changes for basic use) or can be explicitly configured in the spec. Each adapter creates one trace per harness run and one span per stage.

### Activation (env-var driven, no spec changes)

```bash
# LangFuse
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_HOST=https://us.cloud.langfuse.com   # optional

# LangSmith
export LANGSMITH_API_KEY=ls__...
export LANGSMITH_PROJECT=my-project                  # optional
```

If the env vars are present and the package is installed, the adapter auto-registers at harness init.

### Optional spec config (for explicit control)

```yaml
telemetry:
  langfuse:
    host: https://us.cloud.langfuse.com   # override default
    project: armature-prod                # tag for grouping
    enabled: true                         # can set false to suppress even with env vars

  langsmith:
    project: armature-prod
    enabled: true
```

### What gets traced

| Field | Trace level | Span level |
|-------|------------|-----------|
| `run_id` | ✓ | |
| `workflow_name` | ✓ | |
| `spec_version` | ✓ | |
| `stage_id` | | ✓ |
| `role_type` | | ✓ |
| `model` | | ✓ |
| `input_tokens` | | ✓ |
| `output_tokens` | | ✓ |
| `latency_ms` | | ✓ |
| `quorum_score` | | ✓ (judge stages) |
| `success` | ✓ | ✓ |
| `error` | | ✓ (on failure) |

### Implementation Plan

**New optional dep groups:**
```toml
langfuse = ["langfuse>=3.0"]
langsmith = ["langsmith>=0.2"]
```

**New modules:**
- `armature/telemetry/langfuse.py` — `LangFuseAdapter`
- `armature/telemetry/langsmith.py` — `LangSmithAdapter`

Both follow the same pattern:
- `attach(hooks: HookRegistry, run_id: str, spec: HarnessSpec)` — registers hooks
- PRE_RUN: create trace
- PRE_STAGE: create span
- POST_STAGE: close span with metrics
- POST_RUN / on exception: close trace

**Engine change:** `Harness.__init__` checks env vars and calls `adapter.attach(self._hooks, ...)` if applicable.

---

## e) Sandbox Isolation

### Problem
Armature's `shell` and `file_write` built-in tools execute directly on the host. For workflows that process untrusted content or call external APIs that might trigger malicious commands, this is a meaningful risk.

### The Armature Way
An optional `sandbox:` section in the spec. When enabled, `shell` and file-operation tools route through a sandbox provider transparently — stages don't change, tool names don't change. The sandbox is the execution environment, not a different API.

### New YAML

```yaml
sandbox:
  mode: none          # default: current behavior (direct host execution)

# Docker mode:
sandbox:
  mode: docker
  image: python:3.11-slim      # image to use
  timeout_s: 300               # max execution time per tool call
  allow_network: false         # network access inside container
  workspace: /workspace        # path inside container; maps to run's workspace on host
  env:                         # extra env vars injected into container
    MY_SECRET: ${MY_SECRET}
```

### How it works

The `DockerSandboxProvider` intercepts `shell`, `file_write`, and `file_read` tool calls:

- **`shell {cmd}`** → `docker run --rm --network none -v {host_workspace}:{workspace} {image} sh -c "{cmd}"`
- **`file_write {path, content}`** → writes to `{host_workspace}/{path}` directly (bind-mounted)
- **`file_read {path}`** → reads from `{host_workspace}/{path}` directly

The container is ephemeral (one per `shell` call). The workspace directory persists across calls within the same run (shared bind mount at `~/.armature/runs/{run_id}/workspace/`).

### Implementation Plan

**New spec models:**
```python
class SandboxMode(str, Enum):
    NONE = "none"
    DOCKER = "docker"

class SandboxConfig(BaseModel):
    mode: SandboxMode = SandboxMode.NONE
    image: str = "python:3.11-slim"
    timeout_s: float = 300.0
    allow_network: bool = False
    workspace: str = "/workspace"
    env: dict[str, str] = {}

class HarnessSpec(BaseModel):
    ...
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
```

**New module:** `armature/sandbox/docker.py` — `DockerSandboxProvider`

**Registry change:** When `sandbox.mode = docker`, replace the `shell`, `file_write`, and `file_read` tool handlers at harness init with sandbox-wrapped versions.

**Dependency:** Docker daemon must be running. `armature doctor` checks this.

---

## f) Sub-Agent Context Isolation

### Problem
Armature's `subagent_spec` stages pass the full parent context to the child harness. For untrusted sub-workflows, third-party specs, or workflows with sensitive context keys, this is a security and correctness risk.

### The Armature Way
A single boolean flag `isolated: true` on a subagent stage. When set, the child harness receives only the keys explicitly declared in `signature.input` — the parent context is stripped.

### New YAML

```yaml
- id: third_party_analyzer
  subagent_spec: workflows/external-analyzer.yml
  isolated: true               # ← new flag; strip parent context before spawning
  signature:
    input:
      document: document       # only this key is passed; all others are hidden
    output:
      analysis: analysis
  depends_on: [loader]
```

Without `isolated: true`, behavior is unchanged (full context passthrough).

### Implementation Plan

**Spec model change** (`Stage`):
```python
class Stage(BaseModel):
    ...
    isolated: bool = False     # when True + subagent_spec set, strip context to signature.input only
```

**Engine change** (subagent dispatch in `engine.py`):
```python
child_context = context.copy()
if stage.isolated and stage.signature and stage.signature.input:
    child_context = {k: context[v] for k, v in stage.signature.input.items() if v in context}
```

This is a ~5 line change in the engine.

---

## g) Messaging Channel Connectors

### Problem
Armature is triggered by Python code or the CLI. Users on Telegram, Slack, or other platforms have no way to trigger workflows from their native environment without a custom integration for each platform.

### The Armature Way
Channel connectors are a separate long-running process (`armature channels start`). They listen on a messaging platform, route messages to workflow specs via configurable patterns, run the workflow (embedded or via HTTP service), and send the response back. The workflow itself doesn't know or care it was triggered from Telegram vs. the CLI — it just receives a context dict.

### How the agentic team "consumes" messages

The workflow YAML is unchanged. The channel connector injects the message as context variables. A workflow designed for channel use would reference `{{ message }}` (or other mapped variables) in its stage descriptions:

```yaml
# workflows/assistant.yml
stages:
  - id: responder
    role:
      name: Assistant
      type: worker
      description: |
        You are a helpful assistant. Respond to the following message:
        
        From: {{ user_id }}
        Message: {{ message }}
        
        Give a clear, concise response.
```

The channel connector then sends `result["responder"]["content"]` back to the user.

### New config format (`channel.yaml` — separate from workflow spec)

```yaml
armature:
  mode: embedded          # "embedded" (direct import) or "http" (calls service)
  service_url: http://localhost:8080   # only needed for http mode

channels:
  - type: telegram
    bot_token: ${TELEGRAM_BOT_TOKEN}

  # - type: slack
  #   bot_token: ${SLACK_BOT_TOKEN}
  #   app_token: ${SLACK_APP_TOKEN}       # socket mode

routing:
  # Pattern-based: regex applied to message text
  - pattern: "^/research (.+)"
    workflow: workflows/research.yml
    inputs:
      topic: "$1"              # regex capture group 1

  - pattern: "^/analyze (.+)"
    workflow: workflows/analysis.yml
    inputs:
      content: "$1"
      user_id: "{{user_id}}"   # platform-provided metadata

  # Default catch-all
  - default: workflows/assistant.yml
    inputs:
      message: "{{message}}"
      user_id: "{{user_id}}"

# How the workflow result maps back to the reply
reply:
  stage: responder             # which stage's output to send
  field: content               # which field of that output (default: "content")
  # OR:
  template: |
    {{ synthesizer.decision }}
    Confidence: {{ synthesizer.confidence }}
```

### CLI

```bash
armature channels start channel.yaml
armature channels start channel.yaml --daemon   # background process
```

### Implementation Plan

**New module:** `armature/channels/`
- `base.py` — `BaseChannel` abstract class with `start()`, `stop()`, `send(user_id, text)`
- `telegram.py` — `TelegramChannel` (uses `python-telegram-bot`)
- `slack.py` — `SlackChannel` (uses `slack-sdk` socket mode)
- `router.py` — `ChannelRouter` — loads `channel.yaml`, matches patterns, invokes workflows
- `runner.py` — `ChannelRunner` — orchestrates channels + router lifecycle

**New optional deps:**
```toml
channels-telegram = ["python-telegram-bot>=21.0"]
channels-slack = ["slack-sdk>=3.0"]
```

**New CLI command:** `armature channels start <config_path> [--daemon]`

---

## Implementation Order

```
Phase 1 — Quick wins (low complexity, high value):
  f) Sub-agent isolation flag    (~50 lines, spec + engine)
  b) Skills system               (~100 lines, spec + prompt assembler)
  c) armature doctor             (~200 lines, new CLI command)

Phase 2 — Observability:
  d) LangFuse adapter            (~150 lines, new telemetry module + engine hook)
  d) LangSmith adapter           (~100 lines, parallel to LangFuse)

Phase 3 — Ecosystem integration:
  a) MCP server support          (~400 lines, new mcp/ module + spec + engine)

Phase 4 — Infrastructure:
  e) Sandbox isolation           (~300 lines, new sandbox/ module + spec + tool routing)
  g) Channel connectors          (~600 lines, new channels/ module + CLI + config format)
```

Total estimated: ~2,000 lines of new code, all cleanly modular.
