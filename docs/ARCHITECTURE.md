# Armature — Architecture

**Version:** 0.1.0  
**Status:** Production-ready  

---

## The Problem: Agents Are Engines Without Cars

When engineers reach for an LLM to automate something complex, they almost always write the same boilerplate: a loop that calls the model, parses its output, handles failures, logs what happened, and moves to the next step. This scaffolding — the while-loop, context manager, tool dispatcher, state keeper, and safety enforcer — is the real engineering work. The model itself is interchangeable.

The industry calls this scaffolding a **harness**. Frameworks like LangChain, CrewAI, and LangGraph provide building blocks, but they require a human architect to assemble them into a working system for every new use case. That's slow, inconsistent, and doesn't learn from itself.

Armature ships as a **ready-to-run harness** that executes workflow definitions written in structured YAML. You provide the goal; the harness handles orchestration, quality control, failure recovery, observability, and self-improvement.

The analogy: the LLM is the engine. Armature is the car.

---

## Research Foundation

Armature synthesizes nine academic papers published between February and June 2026, plus one industry governance framework, all converging on the same insight: **the harness is more important than the model.**

| Source | Key Contribution |
|--------|-----------------|
| **NLAH** — Tsinghua, [arXiv:2603.25723](https://arxiv.org/abs/2603.25723) | 7-component NL spec format; IHR metric; 47.2% vs 30.4% on OSWorld vs. code harnesses |
| **Meta-Harness** — Stanford, [arXiv:2603.28052](https://arxiv.org/abs/2603.28052) | Trace-based outer optimization loop; causal reasoning over proposal history |
| **AutoHarness** — [arXiv:2603.03329](https://arxiv.org/abs/2603.03329) | NL-to-spec synthesis; harness-as-verifier (judge role ancestry) |
| **AgentSpec** — [arXiv:2503.18666](https://arxiv.org/abs/2503.18666) | Declarative runtime safety rules; pre/post-tool hook architecture |
| **Continual Harness** — [arXiv:2605.09998](https://arxiv.org/abs/2605.09998) | Reset-free self-improvement; 4-code failure taxonomy; inner + outer loops |
| **AHE** — [arXiv:2604.25850](https://arxiv.org/abs/2604.25850) | Prediction-verification accountability; component-level improvement targeting |
| **System Scaling** — [arXiv:2605.26112](https://arxiv.org/abs/2605.26112) | Staleness, provenance, drift score, postcondition verification, component governance |
| **KYA** — Veldt Labs, [arXiv:2605.25376](https://arxiv.org/abs/2605.25376) | Static spec risk scoring; only-tighten safety composition; rogue signal counting |
| **ActiveGraph** — [arXiv:2605.21997](https://arxiv.org/abs/2605.21997) | LLM response caching by content hash; trace-triggered behavior rules; `--auto-improve` gate |
| **Self-Harness** — [arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1 | Causal 3-tuple failure attribution; declared editable surfaces; K-proposal diversity; regression gating |
| **Microsoft AGT** — github.com/microsoft/agent-governance-toolkit | Reversibility classification, tamper-evident traces, human approval gates, strict mode |

The benchmarked insight from NLAH: when workflows are defined in readable, editable natural language rather than code, both humans and optimizers can reason about them causally. A YAML diff is meaningful; a Python AST diff is opaque.

---

## Core Architecture

### The Nine Harness Components

Every production harness must implement all nine of these. Armature ships them all.

| # | Component | What It Does | Armature Implementation |
|---|-----------|-------------|------------------------|
| 1 | **Iteration Loop** | Assemble context → call model → dispatch tool → update state → check termination | `DAGExecutor` + async stage runner |
| 2 | **Context Management** | Keep, summarize, or drop context as it grows | `ContextManager`, prompt assembler pipeline |
| 3 | **Tools, Skills, Registry** | File I/O, HTTP, shell, and higher-level skills (RAG, Quorum (another open source project to consider)) | `ToolRegistry`, `BuiltinSkills` |
| 4 | **Subagents** | Fan out work to N child agents in parallel with isolated context | `SubagentNode`, `asyncio.gather` |
| 5 | **Built-in Skills** | Non-negotiable out-of-box capabilities | SLM/LoRA, RAG, Quorum (another open source project to consider) skills bundled |
| 6 | **Session Persistence** | Append-only event log; runs survive crashes and resume | `SessionLog` (JSONL), `ArtifactStore` |
| 7 | **Prompt Assembly** | Static prefix + spec NL + dynamic context + few-shot examples | `PromptAssembler`, Jinja2 templates |
| 8 | **Lifecycle Hooks** | Pre/post-stage and pre/post-tool injection for policy, logging, cost | `HookRegistry`, `HookDecision` |
| 9 | **Permissions and Safety** | Tool permission levels, destructive action interception, human gates | `PermissionChecker`, `SafetyHookBuilder` |

### The Four Agent Role Types

Every stage declares one of four roles. The harness enforces distinct behavioral contracts for each.

| Role | Model Tier | Behavioral Contract |
|------|-----------|---------------------|
| `worker` | Small/medium SLM | Structured execution, tool calls. Output schema enforced via guided decoding. 80–90% of task volume. |
| `orchestrator` | Frontier | Multi-step planning, routing decisions, full workflow state access. |
| `judge` | Frontier | Quality scoring, consensus deliberation. The only role that can block a workflow from advancing. |
| `researcher` | Frontier or large | RAG synthesis, complex reasoning over retrieved context. |

The harness automatically escalates: if a worker fails schema validation or produces low-confidence output, it re-routes to the next model tier without workflow author intervention.

### Stage Handler Dispatch

When the `DAGExecutor` resolves a stage for execution, `make_handler` selects one of two paths. If the stage carries a `loop: IterationConfig` field, the handler calls `_run_with_loop`, which drives deliberate multi-iteration execution — carrying selected output fields forward across iterations, injecting an `_iteration` counter, and evaluating an optional `until` condition to determine when to stop. Stages without `loop` continue to use `_execute_stage_with_recovery`, which delegates to `_run_with_retry` for `on_fail.loop` (failure-triggered retry) or falls straight through to a single `_execute_stage` call. `IterationConfig` (deliberate iteration) and `LoopConfig` (`on_fail.loop` retry) are sibling Pydantic models in `armature/spec/models.py`; their separation keeps the intent distinct — one is a designed repetition structure, the other is a recovery mechanism.

---

## Workflow Spec Format

Workflows are YAML files. A stage looks like this:

```yaml
name: customer-triage
version: "1.0"

model_tiers:
  small:    { provider: ollama,      model: qwen2.5:7b }
  frontier: { provider: anthropic,   model: claude-opus-4-7 }

stages:
  - id: analyze_request
    role:
      type: worker
      model_tier: small
      description: |
        Parse the incoming customer request. Identify intent, extract entities,
        flag ambiguity. Return structured analysis.
    output_mode: guided_json
    output_schema:
      type: object
      required: [intent, entities, ambiguity_score]
      properties:
        intent:          { type: string }
        entities:        { type: array, items: { type: string } }
        ambiguity_score: { type: number }
    on_fail:
      loop: { max: 3 }

  - id: draft_response
    depends_on: [analyze_request]
    role:
      type: orchestrator
      model_tier: frontier
      description: |
        Draft a response using the analysis. If ambiguity_score > 0.7,
        ask one clarifying question instead.

  - id: quality_check
    depends_on: [draft_response]
    gate: human

  - id: refiner
    post_run: true
    role:
      type: researcher
      model_tier: frontier
      description: |
        Review the transcript and diagnostics. Propose specific wording changes
        to the analyst stage that would prevent recurrence.
```

The `post_run: true` marker enables the in-run adaptation loop from Continual Harness. The refiner stage automatically receives `_transcript` (all prior stage outputs) and `_diagnostics` (structured failure signatures) in its context.

Two additional top-level spec fields are supported. `continuation:` carries named stage outputs across successive runs — `carry_forward` lists `key: <stage>.<field>` pairs and `inject_as` names the dict injected into every stage's context (defaults to `prior_run`). `triggers:` declares a list of `CronTrigger` (with `schedule:`) or `WebhookTrigger` (with `path:`) entries that are validated at spec load time and honored by `armature watch`.

### Parallel Fan-Out

```yaml
stages:
  - id: parallel_research
    subagent_spec: workflows/researcher.yaml
    fan_out: 4
    fan_in: consensus      # LLM judge synthesizes conflicting results
    partition_key: queries # splits context["queries"] across 4 children

  - id: synthesize
    depends_on: [parallel_research]
    role:
      type: orchestrator
      model_tier: frontier
      description: Synthesize findings from all four research streams.
```

Fan-in strategies: `list` (array of child results), `merge` (last-write-wins dict), `first` (race pattern), `consensus` (LLM judge synthesizes conflicting parallel outputs).

### Declarative Safety Rules

```yaml
safety_mode: strict   # deny everything not explicitly allowed

safety_rules:
  - tool: file_read
    condition: { field: path, op: truthy, value: "" }
    action: allow

  - tool: shell
    condition: { field: cmd, op: matches_regex, value: "rm\\s+-rf" }
    action: block
    message: "Destructive rm -rf is not permitted"

  - tool: "*"
    condition: { field: _tool_reversibility, op: equals, value: "none" }
    action: require_approval
    message: "Irreversible tool call — confirm with operator"
```

Operators: `contains`, `not_contains`, `equals`, `not_equals`, `matches_regex`, `truthy`.  
Actions: `allow`, `block`, `warn`, `log`, `require_approval`.  
`_tool_reversibility` is a pseudo-field injected automatically — rules can match on `full`, `partial`, or `none` without knowing specific tool names.

---

## The Self-Improvement Flywheel

This is the strategic differentiator. Each loop compounds the others.

```
                     ┌─────────────────────────┐
                     │   Armature Harness        │
                     │  (executes workflows)     │
                     └────────┬────────────────┘
                              │ records every stage to TraceStore
           ┌──────────────────┴──────────────────┐
           │                                     │
┌──────────▼────────────┐          ┌─────────────▼───────────────┐
│  Inner Loop (in-run)  │          │  Outer Loop (cross-run)      │
│  post_run stage runs  │          │  armature improve <spec>     │
│  after DAG completes  │          │  1. Load 200 recent traces   │
│  — receives           │          │  2. Compute rolling IHR      │
│    _transcript +      │          │  3. DiagnosticAnalyzer       │
│    _diagnostics       │          │  4. SpecRefiner (LLM) →      │
│  — proposes immediate │          │     revised YAML             │
│    refinements        │          │  5. Auto-apply or stage for  │
└──────────┬────────────┘          │     review (pending.yaml)    │
           │                       └─────────────┬───────────────┘
           └──────────────┬──────────────────────┘
                          │
             ┌────────────▼──────────────┐
             │  Loop 2: SLM Fine-tuning  │
             │  armature export-traces   │
             │  High-quality traces →    │
             │  SFT/DPO training data →  │
             │  fine-tune smaller model  │
             └────────────┬──────────────┘
                          │ better workers
             ┌────────────▼──────────────┐
             │  Loop 3: RAG Enrichment   │
             │  Trace failures reveal    │
             │  knowledge gaps → improve │
             │  retrieval index          │
             └───────────────────────────┘
```

**The key property:** the harness gets measurably better the more it runs — without human engineering effort after initial deployment. Traces fuel fine-tuning. Fine-tuned models produce better traces. Better traces improve both improvement loops.

### Accountability in the improvement loop

Every proposed spec revision carries a falsifiable contract:

```json
{
  "predicted_fixes": ["output_invalid:brief_builder"],
  "predicted_regressions": []
}
```

The next cycle verifies predictions against observed diagnostic shift — computing `verified_fixes`, `missed_predictions`, and `unexpected_regressions`. The harness doesn't just improve; it explains itself cycle by cycle.

A **drift score** catches regressions before they compound: `len(regressed) / max(len(current_failures), 1)`, where `regressed` is the intersection of current failures with the set of issues ever previously verified as fixed.

### Component governance

`_classify_changes()` categorizes every proposed spec change:
- **Auto-apply**: descriptions, timeouts, retry limits — safe, reversible
- **Review-required**: adding/removing stages, modifying `safety_rules`, changing `output_schema` required fields

Review-required changes are written to `{spec}.pending.yaml` instead of overwriting the live spec. `armature improve <spec> --apply-pending` promotes a staged revision after human review.

---

## Observability

Every run writes structured, tamper-evident trace records:

| Field | What it captures |
|-------|-----------------|
| `inputs_hash` | SHA-256 of exact stage inputs — verify a trace was not altered |
| `policy_version` | SHA-256 of active `safety_rules` — links each trace to the policy that governed it |
| `inputs_provenance` | Per-key origin map: `"user_input"`, `"stage:id"`, `"memory"`, `"stale_memory"` |
| `error_type` | Failure classification (including `PostconditionFailed`) |

`TraceStore.get_run_outputs(run_id)` returns the named outputs for any completed run; the `continuation:` spec block calls this at run start to inject prior-run values into every stage's context.

**IHR (Implicit Harness Rating):** `0.35 × output_valid_rate + 0.25 × success_rate + 0.20 × avg_quorum_score + 0.10 × latency_score + 0.10 × happy_path_rate`. A well-tuned workflow should score above 0.80.

```bash
armature report --run-id <id>          # per-run text report
armature dashboard <spec>              # Rich 4-panel aggregate dashboard
armature dashboard <spec> --watch      # auto-refresh every 5s
armature dashboard <spec> --format json # machine-readable output
```

---

## Async HTTP Service and the LangGraph Sidecar Pattern

Armature is batch-oriented: a workflow runs start-to-finish and returns a structured result. This makes it a natural sidecar for conversational systems that need to offload multi-stage work.

```
User
 │
 ▼
LangGraph turn loop
 ├─ classify intent
 ├─ [chitchat]  ──→  fast LLM response (100 ms)
 └─ [research]  ──→  POST /run/async → Armature sidecar
                         ├─ gather stage
                         ├─ assess stage
                         └─ synthesize stage
                      ← result dict
                     compose response → user
```

Endpoint shapes:

```
POST /run              # synchronous — blocks until complete
POST /run/async        # 202 Accepted → { job_id }
GET  /run/{job_id}     # poll for completion
GET  /run/{job_id}/events  # SSE stream: stage_start, stage_complete, run_complete
```

A complete LangGraph + Armature template (Docker Compose, bot app, research workflow) lives in `templates/langgraph-sidecar/`. See `docs/INTEGRATION.md` for the full integration guide.

---

## Technology Choices

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Spec validation | Pydantic v2 | Typed models, schema generation, parse-time enforcement |
| YAML loading | `ruamel.yaml` | Preserves comments, handles anchors |
| Template rendering | Jinja2 | `{{variable}}` interpolation in specs |
| LLM routing | `litellm` | Unified interface: Ollama, Anthropic, OpenRouter, etc. |
| Async execution | `asyncio` | Native Python; compatible with FastAPI |
| State store | SQLite | Zero infra; auto-migrated on first `init()` |
| CLI | `typer` | Clean, typed, minimal boilerplate |
| Dashboard | `rich` | Terminal panels, sparklines, live refresh |
| HTTP service | `FastAPI` | Consistent with Python service patterns |
| Testing | `pytest` + `pytest-asyncio` | 1,388 tests, all async-compatible |

---

## Key Design Decisions

**Library-first, service-optional.** Armature ships as a Python library. The FastAPI service is an optional thin wrapper. Most consumers run workflows in-process with `await harness.run(inputs)`.

**YAML + natural language specs.** NLAH benchmarks show 47.2% vs 30.4% on OSWorld compared to code-based harnesses. NL specs are version-controllable, diffable, readable by non-engineers, and feed directly into the optimizer which proposes YAML diffs.

**Four roles as first-class primitives.** `worker`, `orchestrator`, `judge`, `researcher` have distinct behavioral contracts, tool sets, and model routing rules. Making them explicit in the spec enables the harness to enforce role-appropriate constraints automatically.

**SQLite for state.** Zero infrastructure setup for new users. The schema auto-migrates on first use. New columns (e.g., `inputs_hash`, `inputs_provenance`) are added with safe defaults so existing databases load cleanly.

**Unconditional hook architecture.** Every tool call passes through the hook pipeline regardless of safety configuration. The `NONE`/`DOCKER` sandbox modes and `permissive`/`strict` safety modes are decisions about *what the hooks do*, not whether they run.

---

## CLI Reference

```bash
armature run <spec>                         # execute a workflow
armature validate <spec>                    # validate without running
armature new [output]                       # interactive spec wizard
armature doctor                             # environment health check
armature serve                              # start HTTP service
armature optimize <spec>                    # single-shot meta-harness optimizer
armature improve <spec>                     # self-improvement loop
armature improve <spec> --apply-pending     # apply a staged pending.yaml revision
armature report --run-id <id>              # per-run text report
armature dashboard <spec>                   # Rich aggregate health dashboard
armature dashboard <spec> --watch           # auto-refresh
armature dashboard <spec> --format json    # machine-readable JSON
armature export-traces                      # export traces as SFT/DPO training data
armature channels start                     # messaging channel connectors
armature watch <spec>                       # daemon: fire run() on every cron/webhook trigger
```

---

## What's Implemented

All nine papers and the AGT framework are fully implemented. 1,388 tests.

| Research concept | Armature implementation |
|-----------------|------------------------|
| NLAH 7-component spec | YAML spec + Pydantic models |
| Four role types + model routing | Role enum + model tier escalation |
| Parallel fan-out / fan-in | `SubagentNode`, `asyncio.gather`, 4 strategies |
| IHR quality metric | `compute_ihr()`, rolling IHR in `SelfImproveRunner` |
| Structured trace collection | `TraceStore` (SQLite), per-stage records |
| Outer optimization loop | `OptimizerRunner`, `ProposalStore`, `run_loop()` |
| NL-to-spec synthesis | `SpecDrafter`, `AutoHarness` |
| Pre/post-tool hooks | `HookRegistry`, `HookDecision` |
| Declarative safety DSL | `ToolSafetyRule` + `SafetyCondition` |
| 4-code failure taxonomy | `DiagnosticAnalyzer` |
| Cross-run persistent memory | `MemoryStore` |
| In-run refiner stage | `Stage.post_run` |
| Cross-run self-improvement | `SelfImproveRunner`, `SpecRefiner` |
| Fine-tuning bridge | `TraceExporter` (SFT/DPO JSONL) |
| Falsifiable improvement contract | `RefinerResult.predicted_fixes/regressions` |
| Prediction verification | `_verify_predictions()` |
| Reversibility classification | `Reversibility` enum, all builtins classified |
| Tamper-evident traces | `inputs_hash`, `policy_version` on `TraceRecord` |
| Human approval gate | `require_approval` safety action |
| Strict fail-closed mode | `safety_mode: strict` |
| Memory staleness detection | `staleness_threshold_days`, `_stale_memory_keys` |
| Context provenance | `inputs_provenance` on `TraceRecord` |
| Drift score | `ImprovementReport.drift_score` |
| Post-condition verification | `ToolDescriptor.postcondition`, `PostconditionFailed` |
| Consensus fan-in | `fan_in: "consensus"`, `_consensus_judge()` |
| Component governance | `_classify_changes()`, `.pending.yaml` staging |
| Deliberate iteration loop | `IterationConfig`, `_run_with_loop`, `loop:` stage field |

---

*For workflow authoring, see `USER-GUIDE.md`. For integration patterns, see `docs/INTEGRATION.md`. For the hands-on tutorial, see `BUILD_FIRST_WORKFLOW.md`.*
