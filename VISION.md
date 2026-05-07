# Armature — Vision Document

**Version:** 0.1.0  
**Date:** 2026-05-04  
**Status:** Draft — Pending Approval  
**Project:** `/Users/bryansparks/projects/armature`  
**Package name:** `armature`

---

## Executive Summary

Armature is a Python-native agent execution harness. It wraps language models in a structured, inspectable while-loop that executes workflows defined as natural-language-enriched YAML specs — deterministically, repeatably, and with built-in quality controls.

Armature is the connective tissue of a compound intelligence stack. It routes work to SLM workers or frontier model orchestrators, enforces typed output schemas, persists execution state and traces, and feeds back into Alembic (fine-tuning), Quorum (deliberative quality validation), and Tessera (RAG-enriched context). Each loop tightens the others: better traces → better SLMs → better runs → better traces.

Armature ships as a Python library first, enabling any Python project to import and run workflows in-process. An optional FastAPI service wrapper enables language-agnostic HTTP triggering for non-Python consumers.

---

## The Core Concept: What Is a Harness

A harness is **the car around the engine, not the engine itself.** The LLM (Gemma, Qwen, Claude, etc.) is the engine. The harness is the while-loop, context manager, tool dispatcher, state keeper, and safety enforcer that makes the engine do useful, reliable work repeatedly.

Frameworks (LangChain, CrewAI, LangGraph) are collections of building blocks a human architect assembles. A harness ships as a **ready-to-run agent** — you provide the goal and the spec; the harness handles the rest.

The harness is optimized for **autonomous work**. The human defines the workflow spec once, then the harness executes it without requiring human architecture decisions at runtime.

---

## Research Foundations

This design synthesizes four converging lines of research:

| Source | Key Contribution |
|--------|-----------------|
| **Meta-Harness** (Stanford, Khattab et al., arXiv:2603.28052) | Harness-as-program; trace-based outer-loop optimization |
| **NLAH** (Tsinghua, arXiv:2603.25723) | 7-element NL harness spec; IHR; 47.2% vs 30.4% on OSWorld |
| **Harness Survey** (arXiv:2604.0428) | 6-component completeness model (E, T, C, S, L, V) |
| **Video Reference** (nWzXyjXCoCE) | 9 production harness components; reference implementation patterns |

---

## The Nine Core Harness Components

Every harness must implement all nine of these to be production-ready.

### 1. Iteration Loop
The central while-loop governs each agent turn: assemble system prompt → choose tool or action → execute it → update context → check termination condition → repeat. An iteration cap prevents runaway processes. This is the harness heartbeat.

### 2. Context Management
As a workflow runs, context grows. The harness decides what to retain verbatim, what to summarize, and what to drop. Compaction fires when the context approaches the model's token budget — a critical failure mode if unhandled. SLMs with small context windows make this especially load-bearing.

### 3. Tools, Skills, and Registry
**Tools** are primitive actions: read file, write file, run bash command, HTTP call, database query.  
**Skills** are higher-level, domain-specific procedures composed from tools (e.g., `quorum.deliberate`, `tessera.retrieve`, `alembic.submit_trace`).  
Both are registered in a **tool registry** that tracks availability, permissions, and dispatch logic, and exposes descriptors to the model.

### 4. Subagents
For large or parallelizable tasks, the harness fans out work to subagents — child agents with their own sessions, restricted toolsets, and focused system prompts. The harness coordinates fan-out and fan-in. Subagents enable true parallelism without shared state conflicts.

### 5. Built-in Skills
The harness ships non-negotiable primitives out of the box: file I/O, shell execution, HTTP calls, structured output parsing. It also ships higher-level operations: `run_workflow`, `deliberate` (Quorum), `retrieve` (Tessera), `submit_trace` (Alembic). Users are not expected to wire these up themselves.

### 6. Session Persistence / Memory
Long-running or crash-interrupted sessions log every event — messages, tool results, compaction events — to a durable append-only file (JSONL). This enables exact-state replay to resume interrupted workflows. Because the log is append-only, multiple harness runs can share it safely.

### 7. System Prompt Assembly
The system prompt is a **pipeline, not a constant string**. The harness assembles it from:
- Static prefix (stable for prompt caching)
- Harness spec NL sections (roles, phases, contracts, failure modes)
- Dynamic context (current stage, available tools, artifact state)
- Instruction files from the project directory (e.g., `CLAUDE.md`, `HARNESS.md`)

Static content is loaded first to maximize prefix caching across sessions.

### 8. Lifecycle Hooks
Pre- and post-tool hooks allow injecting custom logic without modifying core harness code: policy enforcement, audit logging, observability, cost tracking. Hooks return allow/block/modify decisions. The Alembic trace collector is implemented as a post-tool hook.

### 9. Permissions and Safety
Tools declare required permission levels (`read_only`, `workspace`, `network`, `destructive`). The harness enforces these statically and dynamically classifies shell commands at runtime (e.g., `ls`/`grep` → read-only; `rm`/`sudo`/`shutdown` → destructive). Destructive actions pause for human approval before execution.

---

## The Four Role Types

Armature adds a first-class model routing layer on top of the nine components. Every stage in a workflow declares one of four roles, each with fixed model tier routing:

| Role | Model Tier | Purpose |
|------|-----------|---------|
| `worker` | SLM (tiny/small/medium) | Structured execution, tool calls, routine reasoning. 80-90% of task volume. Guided decoding enforces output schema. |
| `orchestrator` | Frontier | Multi-step planning, complex routing decisions, coordinating across workers. |
| `judge` | Frontier | Quality scoring, output evaluation, Quorum deliberation gateway. Produces the quality signal that filters traces for Alembic. |
| `researcher` | Frontier or Large | Broad knowledge retrieval, Tessera RAG synthesis, novel reasoning. |

The harness escalates automatically: if a `worker` stage fails schema validation or produces confidence below threshold, it re-routes to the next model tier.

---

## Harness Spec Format (NLAH-Inspired YAML)

Workflows are defined as YAML files with 7 structured elements — based on the Natural-Language Agent Harnesses (NLAH) framework — plus Armature extensions:

```yaml
name: deliberation-pipeline
version: "1.0"
description: |
  Research a topic, run structured deliberation, get human approval, publish.

# 1. Contracts — typed I/O, budgets, completion conditions (DSPy-inspired signatures)
contracts:
  inputs:
    - name: topic
      type: str
      required: true
  outputs:
    - name: decision
      schema: DeliberationResult
      required: true
  completion: "deliberation_result.confidence >= 0.8 and approved"
  budget:
    max_iterations: 10
    max_llm_calls: 50
    timeout_hours: 4

# 2. Roles — task-specific system prompts with distinct responsibilities
roles:
  researcher:
    type: researcher
    description: |
      Gather relevant information on the topic. Search broadly, filter for
      credibility and recency. Produce a structured research brief with
      confidence tags per claim.
    tools: [tessera.retrieve, web_search]

  deliberator:
    type: judge
    description: |
      Coordinate Quorum deliberation. Ensure all specialist perspectives are
      heard. Drive toward synthesis confidence >= 0.8.
    skills: [quorum.deliberate]

# 3. Stage structure — explicit DAG topology
stages:
  - id: research
    role: researcher
    signature:
      input: { topic: str }
      output: { brief: ResearchBrief, source_count: int }
    output_mode: guided_json

  - id: deliberate
    depends_on: [research]
    role: deliberator
    signature:
      input: { brief: ResearchBrief }
      output: { decision: str, confidence: float, dissents: list[str] }
    output_mode: guided_json

  - id: verify
    depends_on: [deliberate]
    adapter: confidence_check
    on_fail:
      loop: { stage: deliberate, context: enrich, max: 3 }

  - id: approve
    depends_on: [verify]
    gate: human
    present: "{{deliberate.decision}} (confidence: {{deliberate.confidence}})"
    on_reject:
      loop: { stage: deliberate, context: incorporate_feedback, until: APPROVED }

  - id: publish
    depends_on: [approve]
    adapter: publish_result

# 4. Adapters — deterministic, no AI
adapters:
  confidence_check:
    type: python
    fn: "armature.adapters.checks.confidence"
    args: { threshold: 0.8, field: "deliberate.confidence" }
  publish_result:
    type: script
    cmd: "python publish.py --result-file {{artifacts.deliberation_result}}"

# 5. State semantics — durable artifacts across context resets
state:
  artifacts:
    - name: research_brief
      path: "{{run_id}}/research_brief.json"
    - name: deliberation_result
      path: "{{run_id}}/deliberation_result.json"
  ledger: "{{run_id}}/ledger.jsonl"

# 6. Failure taxonomy — named failure modes with recovery policies
failures:
  low_confidence:
    condition: "deliberate.confidence < 0.8"
    recovery: "Retry deliberation with enriched research. Add contrarian specialist."
    max_retries: 3
  timeout:
    condition: "elapsed > budget.timeout_hours"
    recovery: "Escalate to human gate with best current result."
  schema_violation:
    condition: "output schema validation failed"
    recovery: "Escalate model tier and retry once. Log for Alembic review."

# 7. File-backed state — survives context truncation
file_state:
  enabled: true
  base: "~/.armature/runs/{{run_id}}/"
  workspace: "workspace/"
  manifest: "manifest.json"

# Model tier definitions — project configures, harness routes
model_tiers:
  tiny:     { provider: ollama, model: gemma4:1b }
  small:    { provider: ollama, model: qwen2.5:7b }
  medium:   { provider: openrouter, model: qwen/qwen3.5-72b }
  large:    { provider: openrouter, model: moonshotai/kimi-k2 }
  frontier: { provider: anthropic, model: claude-opus-4-7 }

# Trace config — feeds Meta-Harness optimizer and Alembic
trace:
  enabled: true
  metrics: [confidence, iterations, llm_calls, time_to_complete, human_rounds]
  filesystem: "~/.armature/traces/{{run_id}}/"
  alembic:
    submit_on_completion: true
    min_quality_score: 0.8
    stages: [research, deliberate]
```

---

## The Four-Loop Flywheel

This is the strategic differentiator. Each loop compounds the others:

```
Frontier Models (orchestrators, judges, researchers)
        │ coordinates, scores, enriches context
        ▼
Armature  ────────────────────────────────────────────
  while-loop │ spec-driven │ schema-enforced │ traced
        │
   ┌────┴────────────────────┐
   ▼                         ▼
SLM Workers              Quorum (judge role)
(fast, cheap,            validates outputs,
 guided decoding)        filters trace quality
   │                         │
   └─────────┬───────────────┘
             ▼
    Loop 1: Harness Optimizer (Phase 2)
    Meta-Harness pattern: traces → YAML spec diffs → A/B test → accept
             │
             ▼
    Loop 2: Alembic Fine-Tuning
    High-quality traces → LoRA fine-tune target SLMs
    Better SLMs → better runs → better traces
             │
             ▼
    Loop 3: Tessera RAG Enrichment
    Trace failures reveal knowledge gaps → improve RAG index
    Better RAG context → better SLM outputs → better traces
             │
             ▼
    Loop 4: Quorum Calibration
    More deliberation runs → calibrate priors and specialist weights
    Better deliberation → cleaner quality signal → better Alembic data
```

**Quorum is the quality gate that makes the other loops trustworthy.** Without reliable quality scoring, Alembic trains on noise.

---

## External Consumability

The harness must be consumable by any ELF project and by external services. Two deployment forms:

### Form 1: Python Library (Primary)
```python
from armature import Harness

harness = Harness.from_spec("workflows/deliberation.yaml")
result = await harness.run(topic="AI governance in healthcare")
print(result.decision, result.confidence)
```

Any Python project imports `armature` and runs workflows in-process. No network overhead, direct access to result objects.

### Form 2: HTTP Service (Optional, Phase 2)
A thin FastAPI wrapper exposes the library over HTTP:
```http
POST /run
{ "spec": "deliberation.yaml", "inputs": { "topic": "AI governance" } }

GET /runs/{run_id}/status
GET /runs/{run_id}/result
GET /runs/{run_id}/trace
```

This enables non-Python consumers (future Rust/Go agents, external integrations, Slack bots, n8n, etc.) to trigger harness workflows without importing the library.

---

## Technology Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.11+ | ELF ecosystem native |
| Package manager | `uv` | Fast, modern, deterministic |
| Spec validation | Pydantic v2 | Typed models, JSON schema generation |
| YAML loading | `ruamel.yaml` | Preserves comments, handles anchors |
| Template rendering | Jinja2 | `{{variable}}` interpolation in specs |
| LLM routing | `litellm` | Unified interface: Ollama, Anthropic, OpenRouter, etc. |
| Guided decoding | `outlines` | Token-level schema enforcement for SLMs |
| Async execution | `asyncio` | Native Python async, compatible with FastAPI |
| State store | SQLite (Phase 1) → PostgreSQL (Phase 2) | SQLite requires zero infra; Postgres for multi-process |
| CLI | `typer` | Clean, typed CLI with minimal boilerplate |
| Testing | `pytest` + `pytest-asyncio` | Standard, compatible with ELF patterns |
| HTTP service | `FastAPI` (Phase 2) | Consistent with ELF service pattern |

---

## Integration Points

| ELF Component | Integration |
|---------------|------------|
| **Quorum** | Built-in `quorum.deliberate` skill; `judge` role nodes invoke QuorumEngine; quality scores feed trace filter |
| **Tessera** | Built-in `tessera.retrieve` skill; `researcher` role nodes call Tessera RAG API |
| **Alembic** | Post-execution hook submits high-quality traces as fine-tuning examples; Alembic-tuned SLMs registered as `model_tiers` |
| **ELFAutomations** | Harness workflows orchestrate ELFAutomations service calls (billing, sales, ops); shared PostgreSQL for state |
| **Greenroom** | Greenroom UI can trigger harness runs via HTTP service (Phase 2) |

---

## Phased Roadmap

### Phase 1 — Core Runtime (Current)
Ship a working harness that can execute YAML workflow specs with the nine core components.

**Deliverables:**
- `armature` Python package installable via `pip install armature`
- YAML spec loader + Pydantic models
- Async DAG executor with while-loop core
- LLM node supporting all four role types via litellm
- Script and Python adapter nodes
- Loop-until executor + human approval gate
- Tool/skill registry with built-in primitives
- Append-only session log + file-backed artifact store
- System prompt assembly pipeline
- Lifecycle hooks (pre/post tool)
- Permissions + basic safety classification
- Context management + compaction
- CLI: `armature run <spec.yaml> [--input k=v ...]`
- Quorum integration (built-in judge skill)
- Tessera integration (built-in researcher skill)

### Phase 2 — Optimizer + Service (Next)
Add the self-improvement loop and HTTP service wrapper.

**Deliverables:**
- Trace collector + PostgreSQL store
- Meta-Harness-style optimizer agent
- Alembic trace submission hook
- Subagent support (fan-out/fan-in)
- FastAPI HTTP service wrapper
- Guided decoding via `outlines` for SLM worker nodes
- Uncertainty-aware model tier escalation

### Phase 3 — Spec Editor + Production Hardening

**Armature Editor** — A visual and/or CLI-guided tool for creating and editing workflow specs without writing raw YAML. Because specs are declarative and schema-validated (Pydantic), the editor is purely a generation layer: it produces valid YAML that the runtime already knows how to execute. No runtime coupling required.

Capabilities:
- Workflow graph visualization (DAG rendered as interactive diagram)
- Stage configuration forms (role, signature, output mode, adapters)
- Adapter testing (run a single adapter in isolation before wiring it in)
- Spec validation with human-readable error messages (not Pydantic tracebacks)
- Template library of reusable workflow patterns (deliberation, research, fine-tuning eval, etc.)
- Import/export: generate specs from natural language description via LLM

Analogous to Archon's workflow builder, but domain-agnostic — any Armature workflow, not only coding tasks.

**Production Hardening:**
- Multi-tenant run isolation
- Structured observability (OpenTelemetry traces)
- Managed deployment option (Docker + k8s manifests)
- Role-based permission management

---

## Success Criteria

### Phase 1 Done When:
- [ ] `pip install armature` works from any Python project
- [ ] A `deliberation.yaml` workflow runs end-to-end against Quorum
- [ ] A `research.yaml` workflow runs end-to-end against Tessera
- [ ] A crashed run replays and resumes from its session log
- [ ] CLI runs a workflow from the command line with `--input` args
- [ ] All nine harness components are implemented and tested
- [ ] 80%+ test coverage on runtime core

### Phase 2 Done When:
- [ ] Traces collected from Phase 1 workflows are queryable
- [ ] Optimizer agent proposes spec diffs from trace analysis
- [ ] HTTP service accepts workflow trigger and returns run_id
- [ ] Alembic receives trace submissions from high-scoring runs

---

## Architecture Decisions

### ADR-001: Library-First, Service-Optional
**Decision:** Ship as a Python library. The FastAPI service is optional and built on top.  
**Rationale:** Most ELF consumers are Python. In-process execution avoids network overhead and simplifies dependency management. Service adds complexity that's only needed for external/cross-language consumers.

### ADR-002: SQLite for Phase 1 State
**Decision:** Use SQLite for Phase 1 state and trace storage, migrate to PostgreSQL in Phase 2.  
**Rationale:** SQLite requires zero infrastructure setup for new consumers. PostgreSQL is necessary when multiple processes need to share state, which becomes relevant in Phase 2 with the optimizer and service.

### ADR-003: litellm for LLM Routing
**Decision:** Route all LLM calls through litellm rather than provider-specific SDKs.  
**Rationale:** The model tier system (tiny through frontier) requires seamless switching between Ollama, OpenRouter, Anthropic, etc. litellm provides a unified interface. Provider-specific features (Anthropic prompt caching) can still be accessed via litellm pass-through.

### ADR-004: YAML + NL Spec (Not Pure Code)
**Decision:** Workflow definitions are YAML files with natural language fields, not Python code.  
**Rationale:** NL specs (NLAH research) outperform code-based harnesses on complex tasks (47.2% vs 30.4%). YAML specs are version-controllable, diffable, readable by non-engineers, and feed directly into the Meta-Harness optimizer which proposes YAML diffs.

### ADR-005: Four Role Types as First-Class Primitives
**Decision:** `worker`, `orchestrator`, `judge`, `researcher` are first-class, not just model tier labels.  
**Rationale:** These roles have distinct behavioral contracts, tool sets, and model routing rules. Making them explicit in the spec enables the harness to enforce role-appropriate constraints (e.g., workers always use guided decoding; judges always invoke quality scoring).

### ADR-006: outlines for Guided Decoding (Phase 2)
**Decision:** Defer outlines integration to Phase 2.  
**Rationale:** outlines requires model-level integration that works best with self-hosted models (Ollama). Phase 1 uses strict Pydantic output parsing with retry-on-failure as a simpler reliability mechanism. Phase 2 adds token-level guidance as SLM usage matures.

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| litellm API surface changes | Medium | Medium | Pin litellm version; abstract behind thin adapter |
| SLM output schema violations | High | Low | Retry with prompt engineering; escalate model tier; log for review |
| Context overflow on long workflows | Medium | High | Implement compaction early; make it configurable per spec |
| Quorum/Tessera API changes break built-in skills | Low | High | Skills are thin adapters; pin to stable API versions |
| SQLite write contention in multi-run scenarios | Low | Medium | Document single-process limitation; PostgreSQL migration is Phase 2 |

---

## Open Questions

1. **Quorum integration depth**: Should Armature embed Quorum or call it as a service? ELFAutomations uses it as a library — same approach is likely correct here.
2. **Alembic trace format**: What schema does Alembic expect for fine-tuning examples? Needs alignment before Phase 2.
3. **ARMATURE.md convention**: Should Armature look for an `ARMATURE.md` file in project directories (like `CLAUDE.md`) for project-level instructions injected into system prompts?
4. **Human gate channels**: Phase 1 supports CLI (blocking `input()`). Phase 2 adds Slack/HTTP webhook. Is blocking CLI sufficient for Phase 1, or is an async webhook needed earlier?
5. **PyPI availability**: Verify `armature` is available on PyPI before publishing. Alternative: `armature-harness` if taken.

---

*This document is the authoritative vision for Armature. Update it as implementation reveals insights. Commit all changes to version control.*
