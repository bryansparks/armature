# Armature Gaps Roadmap
## Addressing Weaknesses Surfaced by the AgentCore Comparison

*Written May 2026. Based on analysis in ARMATURE-AGENTCORE.md.*

---

## Context

The AgentCore comparison identified six areas where Armature is thin relative to production needs:

1. Tool ecosystem (discovery, OpenAPI wrapping, governance)
2. Long-term cross-session memory (knowledge extraction, semantic search)
3. Auth (outbound credentials for tool calls)
4. Browser and safe code execution
5. Policy expressiveness
6. Observability depth

This document proposes a concrete implementation plan for each, prioritized by impact vs. effort, and with an honest view of what Armature should build vs. what should simply be delegated to the deployment environment (including AgentCore itself).

---

## Strategic Framing Before the Plan

Not everything here needs to be built inside Armature. There are two valid answers to each gap:

**A. Build it in Armature** — the right choice when the capability connects to Armature's core differentiation (typed DAGs, self-improvement, role specialization, quality control).

**B. Delegate to the deployment environment** — the right choice when the capability is infrastructure (auth, browser sandboxing, managed scaling). Teams deploying on AWS can get these from AgentCore Runtime; teams on other clouds have equivalents. Armature's job is to compose the workflow correctly; the platform's job is to run it safely.

Both answers appear below where relevant.

---

## Priority Ordering

| # | Gap | Impact | Effort | Recommendation |
|---|---|---|---|---|
| P1 | Observability depth | High | Low | Build in Armature |
| P2 | Tool ecosystem | High | Medium | Build in Armature |
| P3 | Long-term memory | High | Medium | Build in Armature |
| P4 | Outbound credentials | Medium | Low | Build in Armature |
| P5 | Browser + safe code execution | Medium | Medium | Build thin; delegate managed to platform |
| P6 | Policy expressiveness | Low | Low-Medium | Extend incrementally |

---

## P1 — Observability Depth

**The gap:** OTel spans are emitted in `_call_with_retry` (model + attempt count), but there are no dashboards, no per-step trace visualization, no live quality scoring on running traces. The `TraceStore` already captures the right raw signals (`quorum_score`, `output_valid`, `latency_ms`, `input_tokens`, `output_tokens`) but there is no way to see them without writing SQL.

**What to build:**

### G6a: LangFuse Integration (~1 day)

`armature/telemetry.py` already initializes the OTel tracer. LangFuse accepts OTel traces via its public endpoint. The entire integration is environment variable configuration — no code changes needed:

```
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(public:secret)>
```

Document this in `docs/observability.md`. For self-hosted LangFuse (Docker Compose), add a reference configuration. This is the fastest path to a production-grade trace dashboard.

### G6b: CLI Trace Report (~2-3 days)

Add two `armature` CLI subcommands:

```
armature traces show --run-id <id> --db <path>
```
Renders a per-stage table: `stage_id | role_type | model | latency_ms | quorum_score | output_valid | input_tokens | output_tokens`

```
armature traces report --db <path> [--workflow <name>]
```
Aggregate quality report across all runs: per-stage averages, IHR trend, output_valid rate, escalation frequency. This surfaces exactly the signals the Optimizer needs, in a human-readable form.

### G6c: Live Evaluation Scoring (~3-4 days)

Connect the existing Alembic skill to a post-run evaluation pass:

- `EvaluationRunner.evaluate(run_id, trace_db)` — loads trace records for a run, scores each stage's output, writes scores back to `TraceStore`
- Scoring criteria come from the spec: `stage.evaluation_criteria: list[str]` (declarative) or `stage.evaluate_fn: str` (dotted Python path to a callable)
- IHR computation (`TraceStore.compute_ihr`) already works; this wires structured per-stage scores into it

**Total effort:** ~1 week. **Why P1:** Observability is table stakes for production. The raw data already exists in `TraceStore`; this is surfacing it.

---

## P2 — Tool Ecosystem

**The gap:** The tool registry holds Python callables registered manually. No way to wrap REST APIs or OpenAPI specs. No semantic tool discovery. No governance workflow. This limits real-world tool integration to tools written as Python functions.

**What to build:**

### G1a: OpenAPI Tool Wrapping (~4 days)

```python
from armature.tools.openapi import register_openapi_tools

register_openapi_tools(
    registry,
    spec="https://api.example.com/openapi.json",  # or local path
    base_url="https://api.example.com",
    auth={"type": "bearer", "env": "EXAMPLE_API_KEY"},
    include=["getUsers", "createOrder"],  # optional whitelist
)
```

Implementation:
- Parse OpenAPI 3.x spec (via `httpx` for URL, or direct dict load for path)
- Generate one `ToolDescriptor` per endpoint: name from `operationId`, description from `summary`, parameters from path + query + request body schema
- Each tool's `dispatch` function makes an `httpx.AsyncClient` call with the declared auth
- Auth types: `bearer` (Authorization header), `api_key` (custom header or query param), `basic`

This is a standalone `armature/tools/openapi.py` module — no changes to the core registry.

### G1b: Semantic Tool Search (~2-3 days)

```python
results = await registry.search("tools that can send email", top_k=3)
```

Implementation:
- At registration time, generate an embedding for each tool's `name + description` string
- Use litellm's embedding endpoint (same config as LLM calls — model-agnostic)
- Store embeddings in memory; recompute on registry change
- `search()` computes cosine similarity, returns ranked `ToolDescriptor` list
- Falls back to substring match if no embedding model is configured

Embedding generation is cheap (< 1ms per tool, ~$0.00001/tool via text-embedding-3-small). This enables agents to discover tools dynamically rather than requiring explicit `role.tools` declarations.

### G1c: Tool Status / Governance (~2 days)

Add `status: Literal["draft", "published", "deprecated"] = "published"` to `ToolDescriptor`. Add `registry.publish(name)` and `registry.deprecate(name)`. By default, agents only see `published` tools. Governance workflow: register as `draft` → review → publish.

This is minimal but gives teams a controlled path for rolling out new tools without exposing untested callables to production agents.

**Total effort:** ~1.5-2 weeks. **Why P2:** The current registry works for small projects but is a real ceiling for real-world deployment. OpenAPI wrapping alone would unlock integration with any existing API surface without writing Python glue code.

---

## P3 — Long-Term Memory (Knowledge Extraction)

**The gap:** `MemoryStore` (already built and wired in the engine) captures a rolling window of raw stage outputs across runs. This is useful but not the same as structured knowledge — facts, preferences, summaries — extracted from those outputs and available for semantic retrieval.

**What exists:**
- `MemoryConfig` + `MemoryCapture` in the spec model — fully wired
- `MemoryStore.record()` / `MemoryStore.load()` — stores raw JSON values per stage/key
- Engine injects captured memories into context at run start as `_memory`

**What's missing:** The extraction layer and semantic retrieval.

**What to build:**

### G3a: KnowledgeStore (~2 days)

```python
# armature/state/knowledge.py
class KnowledgeRecord(BaseModel):
    workflow_name: str
    entity: str          # subject the fact is about (e.g. "user", "project")
    fact: str            # the extracted insight
    confidence: float    # 0.0 – 1.0
    source_run_id: str
    timestamp: str

class KnowledgeStore:
    async def record(self, record: KnowledgeRecord) -> None: ...
    async def load(self, workflow_name: str) -> list[KnowledgeRecord]: ...
    async def search(self, query: str, top_k: int = 5) -> list[KnowledgeRecord]: ...
```

SQLite-backed, same pattern as `TraceStore`. Semantic `search()` uses embeddings (same approach as G1b — litellm embedding endpoint).

### G3b: KnowledgeExtractor (~3 days)

```python
# armature/state/extractor.py
class KnowledgeExtractor:
    def __init__(self, model: str, knowledge_store: KnowledgeStore): ...

    async def extract(
        self,
        memories: dict,         # raw MemoryStore.load() output
        workflow_name: str,
        run_id: str,
    ) -> list[KnowledgeRecord]:
        """Call a small LLM to extract structured facts from raw memories."""
```

The extractor uses a short, focused prompt: given the raw stage outputs (from `MemoryStore.load()`), extract named entities and factual claims. Results are stored in `KnowledgeStore`.

### G3c: Engine Integration (~1 day)

Add `extract_knowledge: bool = False` to `MemoryConfig`. When enabled, the engine calls `KnowledgeExtractor.extract()` at run end (async, non-blocking — extraction failure must never block execution). Add `inject_knowledge_as: str = "_knowledge"` to `MemoryConfig` — at run start, inject relevant facts from `KnowledgeStore.search()` using the workflow name and current context as the query.

**Total effort:** ~1.5 weeks. **Why P3:** Long-term memory directly enables the self-improvement loop. The `BootstrapStore` already pulls high-quality I/O examples from `TraceStore` — `KnowledgeStore` closes the other half: structured knowledge that persists what agents have learned about the domain, the user, and prior task outcomes.

---

## P4 — Outbound Credentials

**The gap:** Tools that call external APIs must manage their own auth. There is no credential injection at the registry level. The LLM tier system handles API keys via `api_key_env` — tools have no equivalent.

**What to build:**

### G4: CredentialStore + Tool Auth Injection (~3-4 days)

```python
# armature/security/credentials.py
class ToolAuth(BaseModel):
    type: Literal["bearer", "api_key", "basic"]
    env: str | None = None           # env var name holding the credential
    header: str = "Authorization"    # header name for injection
    query_param: str | None = None   # alternative: inject as query parameter

class CredentialStore:
    def __init__(self, env_prefix: str = ""): ...
    def get(self, auth: ToolAuth) -> str | None: ...
```

- `ToolDescriptor` gets `auth: ToolAuth | None = None`
- Registry injects credentials into each `dispatch()` call before the tool function sees it — credentials never appear in tool arguments, logs, or context
- `HarnessSpec` gets `credentials: dict[str, str] = {}` for explicit env var → credential name mapping (avoids hard-coding env var names in tool descriptors)

This is not an IdP. It handles the common case: tools that need a static API key or bearer token. OAuth 2.0 flows with refresh are out of scope — recommend using a secrets manager (AWS Secrets Manager, HashiCorp Vault) as the `env` source for those cases.

**Total effort:** ~3-4 days. **Why P4:** Low effort, high practical value. Without this, every tool author is responsible for credential handling — a footgun that will produce credential leakage bugs.

---

## P5 — Browser and Safe Code Execution

**The gap:** `shell_run` executes arbitrary shell commands with no sandboxing, no resource limits, and no network isolation. There is no browser capability.

**Strategic note:** This is the gap where "delegate to the platform" is the most compelling answer. If you deploy Armature workflows inside AgentCore Runtime, you get AgentCore's managed Code Interpreter (sandboxed Python/JS/TS) and Browser tool (managed Playwright with auto-scaling) at no code cost. This is the right answer for AWS-deployed teams.

For teams not on AWS, build the following:

### G5a: Safe Code Execution (~1 week)

Replace `shell_run` with `code_run`:

```python
# armature/tools/sandbox.py
async def code_run(
    language: Literal["python", "bash", "javascript"],
    code: str,
    timeout_s: float = 30.0,
    network: bool = False,        # False = no outbound network
    memory_mb: int = 256,
) -> dict:  # {"stdout": ..., "stderr": ..., "exit_code": ...}
```

Implementation strategy (two tiers, auto-detected):
1. **Docker sandbox** (preferred): `docker run --rm --network none --memory {memory_mb}m --cpus 0.5 ...` — full isolation. Requires Docker daemon.
2. **Process sandbox** (fallback): `asyncio.wait_for` for time limits + `resource.setrlimit` for memory limits (Unix only). Honest about no network isolation.

Keep `shell_run` as a low-level escape hatch with a deprecation notice pointing to `code_run`.

### G5b: Browser Tool (~1 week)

```python
# armature/tools/browser.py
def register_browser_tools(registry: ToolRegistry) -> None:
    """Opt-in; requires: pip install armature[browser]"""
```

Built-in tools registered:
- `browser_navigate(url)` — navigate to URL, return page title + URL
- `browser_extract(selector)` — CSS selector → text content
- `browser_click(selector)` — click element
- `browser_type(selector, text)` — type into form field
- `browser_screenshot()` → base64 PNG (for multimodal models)

One Playwright browser instance per `Harness.run()` call, reused across stages. Torn down on run completion. `asyncio.Lock` for sequential tool calls within a single run (browser state is not thread-safe).

Optional dependency: `playwright` + `armature[browser]` extras.

**Total effort:** ~2 weeks. **Why P5 (not higher):** The platform delegation answer is genuinely better for cloud-deployed workflows. Build the local versions for developers and self-hosted deployments; document AgentCore Browser/Code Interpreter as the production recommendation.

---

## P6 — Policy Expressiveness

**The gap:** `ToolSafetyRule` + `SafetyCondition` handle common patterns (contains, not_contains, regex match) but are not formally verifiable, stateless, and cannot reason about cumulative session behavior.

**Strategic note:** Do not implement Cedar. Cedar is a full policy language with a Rust compiler and formal verification tooling. The implementation cost is high and the use cases requiring it (SOC2, FedRAMP, compliance environments) are better served by deploying Armature inside a platform that enforces Cedar externally (AgentCore Policy, AWS Cedar Authorizer).

**What to build:**

### G6: Callable Policy Rules + Session Policies (~3-4 days)

**Callable rules:**
```python
class PolicyResult(TypedDict):
    action: Literal["allow", "block", "warn"]
    reason: str

def my_policy(tool_name: str, args: dict, context: dict) -> PolicyResult: ...
registry.add_policy(my_policy)
```

Callable policies run alongside YAML rules and can express arbitrary logic — any Python predicate is valid.

**Session-aware rules:**
```python
class SessionPolicy:
    """Stateful policy with access to the full tool call history for the current run."""

    def check(
        self,
        tool_name: str,
        args: dict,
        context: dict,
        call_history: list[ToolCallRecord],  # all prior tool calls in this run
    ) -> PolicyResult: ...
```

Example use cases:
- Block `shell_run` after it has been called more than N times in a session
- Warn if `http_get` has called the same domain more than K times
- Block any tool if cumulative execution time exceeds a threshold

**Total effort:** ~3-4 days. **Why P6 (last):** Current YAML rules handle the most common safety patterns. Callable rules are a low-complexity extension that handles the rest without introducing Cedar's complexity.

---

## Effort Summary

| Priority | Gap | Implementation | Estimated Effort |
|---|---|---|---|
| P1 | Observability | LangFuse config, CLI report, live eval scoring | 1 week |
| P2 | Tool ecosystem | OpenAPI wrapping, semantic search, status | 1.5-2 weeks |
| P3 | Long-term memory | KnowledgeStore, KnowledgeExtractor, engine wiring | 1.5 weeks |
| P4 | Outbound credentials | CredentialStore, ToolAuth injection | 3-4 days |
| P5 | Browser + safe code | code_run sandbox, Playwright tools | 2 weeks |
| P6 | Policy expressiveness | Callable rules, SessionPolicy | 3-4 days |
| **Total** | | | **~8-9 weeks** |

---

## What to Delegate (Not Build)

Some gaps are better answered by pointing to the deployment environment:

| Capability | "Don't build" rationale | Platform answer |
|---|---|---|
| **Inbound auth** | A Python library shouldn't own auth — that belongs in the API gateway or reverse proxy | AgentCore Identity, nginx + OAuth2-proxy, Traefik ForwardAuth |
| **Managed browser at scale** | Session-level Playwright works locally; auto-scaling serverless browser is infrastructure | AgentCore Browser |
| **Sandboxed code at scale** | Docker works locally; managed sandboxing at cloud scale is a product | AgentCore Code Interpreter, E2B |
| **Cedar policy enforcement** | Full formal policy language is overkill without compliance requirements | AgentCore Policy (for AWS deployments) |
| **Long-term memory at scale** | SQLite + local embeddings works well up to millions of records; beyond that, managed vector DBs | AgentCore Memory, Pinecone, Qdrant |

The natural synthesis: **build Armature for workflow composition and self-improvement; deploy it on AgentCore Runtime for production infrastructure.** The gaps labeled "delegate" are precisely what AgentCore handles — the two systems are designed to be complementary.

---

## Recommended Sequencing

If pursuing this roadmap, the suggested order is:

1. **P1 + P4** together — observability and credentials are both low-effort and unblock real production deployments quickly
2. **P2** — tool ecosystem is the most commonly requested capability gap; OpenAPI wrapping alone would unlock a large class of integrations
3. **P3** — long-term knowledge extraction completes the self-improvement loop alongside the already-built `BootstrapStore` and `OptimizerRunner`
4. **P5** — browser and code execution fill the research/automation agent use case; consider this optional if teams are deploying on AWS
5. **P6** — callable policies are a small addition with big flexibility upside; defer Cedar

---

*Last updated: 2026-05-11*
