# Armature: Agent Harness — Current State and Architecture

**Audience:** Engineering leadership  
**Date:** 2026-05-26  
**Author:** Bryan Sparks  
**Status:** Active development — All phases complete (1,198 tests passing)

---

## What This Document Is

This document describes Armature, an agent execution harness built from the ground up to orchestrate AI agent teams doing real work. It covers why I built it, what research and industry frameworks it draws from, how it works, where it currently stands, and — most practically — how to use it when forming agentic teams for automation, research, or decision-support tasks.

---

## The Problem: Agents Are Engines Without Cars

When engineers reach for an LLM to automate something complex, they almost always write the same boilerplate: a loop that calls the model, parses its output, handles failures, logs what happened, and moves to the next step. This scaffolding — the while-loop, context manager, tool dispatcher, state keeper, and safety enforcer — is the real engineering work. The model itself is interchangeable.

The industry calls this scaffolding a **harness**. Frameworks like LangChain, CrewAI, and LangGraph provide building blocks, but they require a human architect to assemble them into a working system for every new use case. That's slow, inconsistent, and doesn't learn from itself.

Armature ships as a **ready-to-run harness** that executes workflow definitions written in a structured YAML format. You provide the goal; the harness handles orchestration, quality control, failure recovery, observability, and optimization.

The analogy: the LLM is the engine. Armature is the car.

---

## Research Foundation

Armature synthesizes seven academic papers published between February and May 2026, plus one industry governance framework released in 2025, all converging on the same insight: **the harness is more important than the model.**

### Paper 1: Natural-Language Agent Harnesses (NLAH)
**Tsinghua University, March 2026** — arXiv:2603.25723

The paper that defines the architectural model. NLAH establishes that workflows defined in structured natural language outperform equivalent code-based harnesses on complex benchmark tasks (47.2% vs. 30.4% on OSWorld). The key finding: when the harness is readable and editable text, the entire system — including an optimizer — can reason about it.

NLAH specifies seven harness components: Contracts, Roles, Stages, Adapters, State, Failure Taxonomy, and File-backed State. Armature implements all seven. The paper also defines IHR (Implicit Harness Rating), a composite quality metric used to score run quality objectively.

NLAH also specifies parallel fan-out as a core orchestration primitive — dispatching N child harnesses simultaneously then recombining their outputs — which is fully implemented in Armature.

**Armature contributions from this paper:** YAML spec format, four role types, DAG executor, IHR computation, parallel fan-out/fan-in with configurable partition and merge strategies.

### Paper 2: Meta-Harness — Automated Optimization End-to-End
**Stanford University, March 2026** — arXiv:2603.28052

The paper behind Armature's optimizer. Meta-Harness introduces an outer optimization loop where a frontier model reads execution traces from prior runs and proposes improvements to the harness spec itself. The critical finding: giving the optimizer access to full execution traces (not just pass/fail scores) improves accuracy from 41% to 57% on benchmark tasks. The optimizer reasons causally about why runs succeeded or failed and proposes targeted edits.

The paper's key insight on multi-iteration optimization: giving the optimizer access to the full history of prior proposals — what was tried, whether it was accepted, and what the score was — enables causal reasoning that avoids re-proposing failed changes and compounds on successful ones.

**Armature contributions:** `OptimizerRunner` (3-stage: analyze → propose → evaluate), A/B spec testing by IHR comparison, `ProposalStore` (SQLite-backed proposal history), `run_loop()` for N-iteration optimization with causal reasoning.

### Paper 3: AutoHarness — LLM-Synthesized Harnesses
**February 2026** — arXiv:2603.03329

AutoHarness demonstrates that LLMs can write their own harness code through iterative refinement — producing harnesses that outperform larger models without harnesses. The insight most applicable to Armature: the concept of a **harness-as-verifier**, where the harness validates that agent outputs meet domain-specific legality constraints before accepting them. This is the conceptual ancestor of Armature's judge/RaaS role. The paper also introduced the synthesis loop: given a natural-language task description, generate a spec, run it, evaluate it, and refine — iterating until quality passes a threshold.

**Armature contributions:** `SpecDrafter` (NL → YAML spec synthesis), `AutoHarness` synthesis loop (generate → run → evaluate → refine, up to N iterations), `PromptBootstrapper` (inject high-quality trace examples as few-shot prompts into spec descriptions).

### Paper 4: AgentSpec — Runtime Enforcement for Safe Agents
**March 2025** — arXiv:2503.18666

The safety paper. AgentSpec introduces a declarative rule language for constraining agent behavior at runtime: "before this tool call, if this condition is true, stop/ask/correct." The rules are composable, lightweight (sub-millisecond evaluation), and can themselves be generated by LLMs. Armature implements the full AgentSpec enforcement architecture: pre/post-tool hooks wired into the engine, and a declarative condition DSL (`ToolSafetyRule` + `SafetyCondition`) that workflow authors write directly in YAML.

**Armature contributions:** `HookRegistry` (pre/post-stage and pre/post-tool), `ToolSafetyRule` + `SafetyCondition` declarative YAML DSL (6 operators: `contains`, `not_contains`, `equals`, `not_equals`, `matches_regex`, `truthy`; actions: `block`, `warn`, `log`), `ToolBlocked` non-retryable exception type. *Extended by AGT concepts — see below.*

### Paper 5: Continual Harness — Reset-Free Self-Improvement for Agentic Systems
**May 2026** — arXiv:2605.09998

The newest paper and the one that closed the self-improvement loop. The Continual Harness argues that agentic systems should improve **continuously** — without human intervention, without resets, and without requiring new training runs. The key architectural insight is a **two-loop design**: an inner loop handles in-run adaptation (a refiner stage runs after the main workflow completes, analyzing what just happened and proposing immediate adjustments) and an outer loop handles cross-run learning (accumulated traces drive diagnostic analysis and spec refinement between runs).

The paper formalizes a **failure signature taxonomy** — four codes that categorize every run's failure modes in a way that can be used to drive targeted remediation:

- `stage_failed` — a stage did not complete (timeout, error, max retries exhausted)
- `output_invalid` — stage output failed schema validation
- `low_confidence` — a judge stage returned a quorum score below threshold (< 0.30)
- `high_escalation` — a stage retried two or more times (indicating fragile conditions)

The paper also addresses the memory architecture: persistent cross-run memory by default (the harness remembers what it learned), with an explicit `fresh` override for cases where a clean slate is required.

Finally, the paper argues that high-quality execution traces from frontier models are a training corpus — not just an observability artifact. When a judge tier runs at frontier quality, every accepted output is a demonstration that can be used to fine-tune a smaller specialist model to do the same work cheaper and faster.

**Armature contributions:**
- `DiagnosticAnalyzer` — extracts the four failure signatures from `TraceRecord` sets; used by the report builder and the self-improvement runner
- `MemoryConfig.fresh: bool` — skip loading prior cross-run memories for a clean-slate run; persistent is the default
- `Stage.post_run: bool` — marks a stage as an in-run refiner; runs after all normal stages complete with `_transcript` + `_diagnostics` injected into context
- `SelfImproveRunner` — the outer self-improvement loop: load traces → compute rolling IHR → run `DiagnosticAnalyzer` → call `SpecRefiner` (frontier LLM rewrites targeted YAML sections) → auto-apply revised spec → write audit log (`armature improve <spec>` CLI command)
- `TraceExporter` — the fine-tuning bridge: export high-quality traces (by quorum score) as SFT training data (chat/alpaca/sharegpt formats) or DPO preference pairs (chosen/rejected matched by stage) for LoRA fine-tuning smaller models (`armature export-traces` CLI command)
- Failure signatures section in `armature report` output — makes diagnostic analysis human-readable

### Paper 6: Agentic Harness Engineering (AHE) — Observability-Driven Automatic Evolution
**April 2026** — arXiv:2604.25850

The accountability paper. AHE introduces a framework for automatically evolving a coding-agent harness through observed execution traces, improving Terminal-Bench 2 pass@1 from 69.7% to 77.0% across ten iterations — with the evolved harness transferring to other benchmarks and model families without re-running the evolution process.

The paper's most distinctive contribution is the **prediction-verification loop**: every proposed harness modification must include a falsifiable contract declaring which tasks it expects to fix (`predicted_fixes`) and which might temporarily worsen (`predicted_regressions`). The next iteration verifies those predictions against observed outcomes using precision/recall metrics. This converts the improvement loop from "did the score go up?" to "did the change do what we said it would?" — a fundamentally different standard of accountability.

AHE also formalizes a **component-level view** of the harness: seven orthogonal editable components (system prompt, tool descriptions, tool implementations, middleware, sub-agent configuration, skills, long-term memory), each at a fixed mount point. The critical ablation finding: these components are non-additive. Long-term memory evolution alone yielded +5.6 pp; tools alone +3.3 pp; middleware alone +2.2 pp; but system prompt evolution *alone* caused a -2.3 pp regression. Evolving multiple components simultaneously underperforms the sum of parts because they pursue overlapping solutions. The implication: improvement cycles should target one component at a time, and role descriptions (equivalent to system prompt) should be the last target, not the first.

**Armature contributions from this paper:**
- `RefinerResult` dataclass — replaces the bare `(HarnessSpec, str)` tuple from `SpecRefiner.refine()`; carries the parsed spec, raw YAML text, `predicted_fixes`, and `predicted_regressions`
- `---PREDICTIONS---` block in `_REFINER_SYSTEM` prompt — instructs the LLM to append a JSON falsifiable contract after the revised YAML declaring which `code:stage_id` failure signatures it expects to resolve and which might worsen
- `SelfImproveRunner._verify_predictions()` — set-math verification on each cycle: computes `verified_fixes` (predicted signatures that are now gone), `missed_predictions` (predicted fixes still present), and `unexpected_regressions` (new signatures not predicted)
- `SelfImproveRunner._load_last_log_entry()` — reads the previous JSONL log entry to retrieve prior predictions and the prior diagnostic state
- Five new fields on `ImprovementReport` — `predicted_fixes`, `predicted_regressions`, `verified_fixes`, `missed_predictions`, `unexpected_regressions`
- `diagnostics_keys` field in the audit log — stores `code:stage_id` strings so the next cycle has the "before" diagnostic state for verification arithmetic

### Industry Framework: Microsoft Agent Governance Toolkit (AGT)
**Microsoft Research, 2025** — [github.com/microsoft/agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit)

The governance framework that extended Armature's safety layer from reactive blocking to principled, auditable governance. AGT formalizes five concepts that the academic papers did not address: how tool calls should be classified by their undo-ability, how execution traces can be made tamper-evident, how policy changes can be linked to the traces they governed, how human sign-off should be integrated into the tool call path, and how a harness should fail when no explicit rule matches.

AGT's full architecture includes an identity/trust-mesh layer and privilege-ring enforcement that goes well beyond what Armature needs today. Armature borrows the *concepts* as first-class citizens, not AGT as a dependency. The five borrowed concepts:

1. **Reversibility classification** — every tool call has an inherent undo-ability: reads are `FULL` (harmless), overwrites are `PARTIAL` (can be recovered), external sends and deletes are `NONE` (irreversible). AGT frames safety rules around this dimension rather than just permission levels.

2. **Arguments hash** — a tamper-evident SHA-256 fingerprint of the exact inputs a stage received. If a trace is later questioned, you can verify it was not altered.

3. **Policy version** — a fingerprint of the `safety_rules` list active at the time a stage ran. When rules change between deployments, auditors can reconstruct which policy governed which traces.

4. **`require_approval` action** — a distinct safety rule outcome between `block` (always deny) and `warn` (always allow). The harness pauses, prints context, and prompts a human operator for `y/N` before proceeding.

5. **Strict mode** (`safety_mode: strict`) — the harness defaults to **deny** when no safety rule matches, instead of the permissive default of **allow**. Paired with an `allow` action type for explicit whitelisting, this gives operators a fail-closed posture.

**Armature contributions from this framework:**
- `Reversibility` enum (`FULL / PARTIAL / NONE`) in `armature/permissions/permissions.py`
- `reversibility` field on `ToolDescriptor`; all 8 built-in tools classified
- `_tool_reversibility` pseudo-field injected into safety rule condition evaluation — rules can match on reversibility without knowing tool names
- `inputs_hash` (32-char SHA-256) and `policy_version` (12-char SHA-256) added to `TraceRecord`; SQLite DB migrated automatically on first `init()`; old DBs without the columns load cleanly
- `HookDecision.REQUIRE_APPROVAL` added to the decision enum
- `ToolSafetyRule.action` expanded to `"block" | "warn" | "log" | "require_approval" | "allow"`
- `HarnessSpec.safety_mode: "permissive" | "strict"` top-level field
- `SafetyHookBuilder.register()` accepts `tool_registry` and `strict_mode`; handles all five actions; returns `BLOCK` on no-match when strict; short-circuits on `allow` before inspecting further rules

### Paper 7: From Model Scaling to System Scaling
**May 2026** — arXiv:2605.26112

The systems paper that shifts the optimization target from model quality to harness architecture. The paper formalizes a six-component framework P_H = Φ(ℛ, ℳ, 𝒞, 𝒮, 𝒪, 𝒢) — Roles, Memory, Context, Skills, Observability, and Governance — and identifies three failure modes that the prior papers left unaddressed:

- **"Stale-but-confident" (ℳ):** The harness injects memory entries that have grown stale, giving the LLM outdated facts with no indication of their age.
- **"Exposure without access" (𝒞):** The harness passes context values to stages without recording where those values came from — making it impossible to audit the chain of information transfer after the fact.
- **"Confident-but-unchecked" (𝒮):** Tool calls that succeed without exception are assumed to have had their intended effect — but no mechanism verifies the actual side effect.

The paper also argues that system-level regression — a previously-fixed failure returning in a later improvement cycle — is more damaging than novel failure, because it erodes trust in the improvement loop itself. It proposes a drift score to surface this directly.

Finally, the paper addresses governance: not all spec changes should be auto-applied. Structural changes (adding stages, modifying safety rules) require human review, while tactical changes (adjusting timeouts, softening descriptions) can be applied immediately. A well-governed harness writes proposed structural changes to a staging file, not to the live spec.

**Armature contributions from this paper:**
- `MemoryStore.staleness_threshold_days` — age threshold (default 30 days) beyond which a memory entry is flagged as stale; stale entries are returned in a `set[tuple[str, str]]` alongside the memory dict
- `_stale_memory_keys` context injection — when stale keys are present at run start, a warning list is injected into context so the LLM prompt surface the fact explicitly
- `TraceRecord.inputs_provenance` — `dict[str, str]` mapping every context key to its origin label: `"user_input"`, `"stage:{stage_id}"`, `"memory"`, or `"stale_memory"`; persisted to SQLite alongside each trace
- `ImprovementReport.drift_score` — `len(current_failures ∩ ever_verified) / max(len(current_failures), 1)`; `_load_all_verified_fixes()` scans the full JSONL history (not just the last cycle) to build the cumulative set of ever-verified fixes
- `ToolDescriptor.postcondition` — optional `Callable[[dict, Any], bool]`; called after every tool dispatch; `PostconditionFailed` exception raised on `False`; caught by the engine and recorded as `error_type="PostconditionFailed"`; `POSTCONDITION_FAILED` added to `DiagnosticCode`
- `fan_in: "consensus"` — new fan-in strategy for subagent stages; parallel results are forwarded to `_consensus_judge()`, an async litellm call that synthesizes conflicting outputs into a single best answer
- `_classify_changes(old_spec, new_spec)` — classifies every proposed spec change as auto-apply (descriptions, timeouts, retry limits) or review-required (stage additions/removals, `output_schema` changes, `safety_rules` modifications); review-required changes are written to `{spec}.pending.yaml` instead of overwriting the live spec; `ImprovementReport.requires_review` and `pending_path` fields surface this to callers

---

## Architecture Overview

### The Nine Core Components

Every production harness must implement all nine of these. Armature ships them all.

| # | Component | What It Does | Armature Implementation |
|---|-----------|-------------|------------------------|
| 1 | **Iteration Loop** | The core while-loop: assemble context → call model → dispatch tool → update state → check termination | `DAGExecutor` + async stage runner |
| 2 | **Context Management** | Decides what to keep, summarize, or drop as context grows | `ContextManager`, prompt assembler pipeline |
| 3 | **Tools, Skills, Registry** | Primitives (file I/O, HTTP, shell) and higher-level skills (SLM/LoRA, RAG, RaaS) | `ToolRegistry`, `BuiltinSkills` |
| 4 | **Subagents** | Fan out work to N child agents in parallel with isolated context and focused roles | `SubagentNode`, `asyncio.gather`, `fan_out`/`fan_in`/`partition_key` |
| 5 | **Built-in Skills** | Non-negotiable out-of-box capabilities: retrieval, deliberation, trace submission | SLM/LoRA, RAG, RaaS skills bundled |
| 6 | **Session Persistence** | Append-only event log so runs survive crashes and can be resumed | `SessionLog` (JSONL), `ArtifactStore` |
| 7 | **Prompt Assembly** | Assembles system prompt from static prefix + spec NL + dynamic context + few-shot examples | `PromptAssembler`, Jinja2 templates, `PromptBootstrapper` |
| 8 | **Lifecycle Hooks** | Pre/post-stage and pre/post-tool injection points for policy, logging, cost tracking | `HookRegistry`, `HookDecision`, declarative `ToolSafetyRule` |
| 9 | **Permissions and Safety** | Tool permission levels, destructive action interception, human approval gates | `PermissionChecker`, `HumanGateNode`, `SafetyHookBuilder` |

### The Four Agent Role Types

Every stage in a workflow declares one of four roles. The harness enforces distinct behavioral contracts for each.

| Role | Model Tier | Behavioral Contract |
|------|-----------|---------------------|
| `worker` | Small/medium SLM (Qwen, Gemma) | Structured execution, tool calls. Output schema enforced via guided decoding. 80–90% of task volume runs here. Cheap and fast. |
| `orchestrator` | Frontier (Claude Opus) | Multi-step planning, routing decisions, coordinating across workers. Knows the full workflow state. |
| `judge` | Frontier | Quality scoring, evaluation, RaaS deliberation. The only role that can block a workflow from advancing. |
| `researcher` | Frontier or large | RAG synthesis, complex reasoning over retrieved context. |

The harness automatically escalates: if a worker stage fails schema validation or produces low-confidence output, it re-routes to the next model tier and retries. This happens without any workflow author intervention.

### How a Workflow Is Defined

Workflows are YAML files. A stage might look like this:

```yaml
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
        intent:    { type: string }
        entities:  { type: array, items: { type: string } }
        ambiguity_score: { type: number }
    on_fail:
      loop: { max: 3 }          # retry up to 3 times with error context injected

  - id: draft_response
    depends_on: [analyze_request]
    role:
      type: orchestrator
      model_tier: frontier
      description: |
        Draft a response to the customer using the analysis. Be concise.
        If ambiguity_score > 0.7, ask one clarifying question instead.
    output_mode: guided_json

  - id: quality_check
    depends_on: [draft_response]
    gate: human                  # pause here for human approval before sending

  - id: refiner
    post_run: true               # runs after all normal stages complete
    role:
      type: researcher
      model_tier: frontier
      description: |
        Review the transcript and diagnostics. Identify what went wrong.
        Propose specific wording changes to the analyst stage description
        that would prevent recurrence.
```

The `post_run: true` marker is the in-run adaptation loop from Continual Harness. The refiner stage receives the full `_transcript` (all prior stage outputs) and `_diagnostics` (structured failure signatures) in its context automatically.

### Parallel Fan-Out

Stages that dispatch child workflows support N-way parallel execution.

```yaml
stages:
  - id: parallel_research
    subagent_spec: workflows/researcher.yaml
    fan_out: 4                  # launch 4 child harnesses in parallel
    fan_in: merge               # merge all child result dicts (last-write-wins)
    partition_key: queries      # split context["queries"] list across the 4 children

  - id: synthesize
    depends_on: [parallel_research]
    role:
      type: orchestrator
      model_tier: frontier
      description: Synthesize findings from all four research streams.
```

Fan-in strategies: `list` (default) — array of child results; `merge` — last-write-wins dict; `first` — first child only (race pattern).

### Declarative Safety Rules

```yaml
# Fail-closed: deny everything not explicitly allowed
safety_mode: strict

safety_rules:
  # Whitelist reads (required in strict mode)
  - tool: file_read
    condition: { field: path, op: truthy, value: "" }
    action: allow

  # Block shell by default
  - tool: shell_exec
    condition:
      field: cmd
      op: matches_regex
      value: "rm\\s+-rf"
    action: block
    message: "Destructive rm -rf is not permitted"

  # Require human sign-off for any irreversible tool call
  - tool: "*"
    condition:
      field: _tool_reversibility
      op: equals
      value: "none"
    action: require_approval
    message: "Irreversible tool call — confirm with operator"
```

Supported operators: `contains`, `not_contains`, `equals`, `not_equals`, `matches_regex`, `truthy`.

Actions: `allow` (explicit whitelist, returns immediately), `block` (raises `ToolBlocked`, not retried), `require_approval` (prompts operator; allow on `y`, block on `n`), `warn` (Python warning, continues), `log` (structured log, continues).

The `_tool_reversibility` pseudo-field is injected automatically — rules can match on `full`, `partial`, or `none` without knowing specific tool names. The `safety_mode: strict` top-level field flips the default from allow-on-no-match to deny-on-no-match.

All five actions and the strict-mode default are concepts borrowed from Microsoft's [Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit).

### Async HTTP Service and the LangGraph Sidecar Pattern

Armature is **batch-oriented**: a workflow runs from start to finish and returns a structured result. This makes it a natural fit as an async sidecar for chatbot systems that need to offload heavy multi-stage work.

The recommended integration pattern: LangGraph (or any state machine) owns the conversational loop and intent routing; Armature owns multi-stage orchestration when a request requires deep work. The two systems are loosely coupled via HTTP — no shared process, no shared imports.

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

The HTTP service exposes three endpoint shapes:

```
POST /run              # synchronous (blocks; use for batch jobs)
POST /run/async        # 202 Accepted → { job_id }
GET  /run/{job_id}     # poll for completion
GET  /run/{job_id}/events  # SSE stream: stage_start, stage_complete, run_complete
```

The SSE stream lets a bot forward progress tokens to the user immediately — `"Researching..."` appears within milliseconds while the Armature stages run in the background. A complete LangGraph + Armature template (Docker Compose, bot app, research workflow) lives at `templates/langgraph-sidecar/`. See `docs/INTEGRATION.md` for positioning guidance and endpoint reference.

### The Self-Improvement Flywheel

This is the strategic differentiator. Each loop makes the others more effective. With the addition of `SelfImproveRunner` (Paper 5), all four loops are now implemented.

```
                        ┌─────────────────────────┐
                        │   Armature Harness        │
                        │  (executes workflows)     │
                        └────────┬────────────────┘
                                 │
             records every stage to TraceStore (SQLite)
                                 │
              ┌──────────────────┴──────────────────┐
              │                                     │
   ┌──────────▼────────────┐          ┌─────────────▼───────────────┐
   │  Inner Loop (in-run)  │          │  Outer Loop (cross-run)      │
   │  post_run stage runs  │          │  armature improve <spec>     │
   │  after DAG completes  │          │  1. Load 200 recent traces   │
   │  — sees _transcript   │          │  2. Compute rolling IHR      │
   │  — sees _diagnostics  │          │  3. DiagnosticAnalyzer       │
   │  — proposes immediate │          │  4. SpecRefiner (frontier    │
   │    refinements        │          │     LLM) → revised YAML      │
   └──────────┬────────────┘          │  5. Auto-apply to spec file  │
              │                       │  6. Append to audit log      │
              │                       └─────────────┬───────────────┘
              │                                     │
              └──────────────┬──────────────────────┘
                             │
                ┌────────────▼──────────────┐
                │  Loop 2: SLM Fine-tuning  │
                │  armature export-traces   │
                │  High-quality traces →    │
                │  SFT/DPO training data →  │
                │  LoRA fine-tune SLMs →    │
                │  register as model tier   │
                └────────────┬──────────────┘
                             │ better workers
                ┌────────────▼──────────────┐
                │  Loop 3: RAG              │
                │  Trace failures reveal    │
                │  knowledge gaps → improve │
                │  retrieval index          │
                └────────────┬──────────────┘
                             │ richer context
                ┌────────────▼──────────────┐
                │  Loop 4: RaaS             │
                │  Calibrate deliberation   │
                │  priors from outcomes →   │
                │  cleaner quality signal   │
                └───────────────────────────┘
```

**The key property:** the harness gets measurably better the more it runs — without any human engineering effort after initial deployment. Traces fuel fine-tuning. Fine-tuned models produce better traces. Better traces improve both the spec optimizer (outer loop) and the in-run refiner (inner loop). Better specs produce better traces.

---

## Current Implementation State

All planned phases are complete. 1,198 tests, passing.

### What's Built

| Capability | Status | Notes |
|---|---|---|
| YAML spec loader + Pydantic validation | ✅ Done | Full schema validation on load |
| Cross-stage typed signature validation | ✅ Done | Spec-parse-time enforcement |
| Async DAG executor (parallel stages) | ✅ Done | Kahn's algorithm, `asyncio.gather` |
| All 4 role types (worker/orchestrator/judge/researcher) | ✅ Done | |
| Guided JSON output (litellm `response_format`) | ✅ Done | Schema enforcement for all LLM calls |
| Model tier routing + auto-escalation | ✅ Done | Escalates on parse failure |
| `on_fail` retry loop with context enrichment | ✅ Done | `_retry_attempt`, `_last_error` injected |
| LLM retry with exponential backoff | ✅ Done | Handles rate limits, service unavailability |
| Human approval gate | ✅ Done | Blocking CLI gate |
| Script/Python adapter nodes | ✅ Done | Deterministic steps alongside LLM steps |
| SubagentNode — parallel fan-out / fan-in | ✅ Done | `fan_out`, `fan_in`, `partition_key`; `asyncio.gather` |
| Session log (JSONL append) | ✅ Done | Full event history per run |
| Artifact store | ✅ Done | File-backed output persistence |
| TraceStore (SQLite) | ✅ Done | Structured per-stage trace records |
| IHR computation | ✅ Done | 4-component quality metric: output validity, success rate, quorum, latency |
| Harness optimizer workflow | ✅ Done | 3-stage: analyze → propose → evaluate |
| A/B spec testing | ✅ Done | IHR-based comparison |
| Multi-iteration optimizer (`run_loop`) | ✅ Done | `ProposalStore` SQLite history; causal reasoning |
| Lifecycle hooks — pre/post stage and tool | ✅ Done | `HookDecision.BLOCK` / `ALLOW` / `REQUIRE_APPROVAL` |
| Declarative safety rules (YAML) | ✅ Done | `ToolSafetyRule` + `SafetyCondition`; 6 operators; 5 actions: block/warn/log/require_approval/allow |
| `ToolBlocked` exception (non-retryable) | ✅ Done | Distinct from stage failures; recovery loop skips retry |
| Tool reversibility classification | ✅ Done | `Reversibility` enum (FULL/PARTIAL/NONE) on `ToolDescriptor`; all 8 builtins classified; queryable as `_tool_reversibility` pseudo-field in safety rules |
| `safety_mode: strict` | ✅ Done | Fail-closed mode — deny-on-no-match instead of allow; `action: allow` for explicit whitelisting |
| `require_approval` safety action | ✅ Done | Pauses execution and prompts operator for y/N before allowing irreversible tool calls |
| Trace argument hashing | ✅ Done | `inputs_hash` (SHA-256 of stage inputs) in every `TraceRecord`; tamper-evident |
| Trace policy version | ✅ Done | `policy_version` (SHA-256 of active `safety_rules`) in every `TraceRecord`; links traces to governing policy |
| OpenTelemetry instrumentation | ✅ Done | Optional; no-op when SDK not installed |
| FastAPI HTTP service — sync | ✅ Done | `armature serve`; `POST /run` blocks until complete |
| FastAPI HTTP service — async | ✅ Done | `POST /run/async` (202), `GET /run/{id}` poll, `GET /run/{id}/events` SSE stream |
| LangGraph sidecar template | ✅ Done | Docker Compose: LangGraph bot + Armature service; classify → chitchat or research; latency acknowledgement pattern |
| Knowledge extraction | ✅ Done | Post-stage LLM extracts structured facts into `KnowledgeStore` |
| Declarative evaluation (eval stages) | ✅ Done | Inline scoring/comparison stages in spec |
| Prompt bootstrapping | ✅ Done | `PromptBootstrapper` injects high-quality trace examples as few-shot prompts |
| AutoHarness synthesis loop | ✅ Done | NL → spec → run → evaluate → refine; iterates to threshold |
| `armature report` command | ✅ Done | Human-readable run report: IHR, stage results, failure signatures |
| `DiagnosticAnalyzer` | ✅ Done | 4-code failure signature taxonomy extracted from trace records |
| `MemoryConfig.fresh` | ✅ Done | Skip loading prior memories for a clean-slate run |
| `Stage.post_run` | ✅ Done | In-run refiner stage: receives `_transcript` + `_diagnostics` after DAG completes |
| `TraceExporter` | ✅ Done | Export traces as SFT (chat/alpaca/sharegpt) or DPO pairs; `armature export-traces` |
| `SelfImproveRunner` | ✅ Done | Outer self-improvement loop; auto-applies revised spec; `armature improve` |
| Improvement audit log | ✅ Done | JSONL log of every analysis cycle (IHR, diagnostics, applied flag) |
| Prediction-verification loop | ✅ Done | `RefinerResult` carries `predicted_fixes`/`predicted_regressions`; each cycle verifies prior predictions against observed diagnostic shift |
| Memory staleness detection | ✅ Done | `MemoryStore.staleness_threshold_days` (default 30 days); stale entries surface as `set[tuple]`; `_stale_memory_keys` injected into run context |
| Context provenance tracking | ✅ Done | `TraceRecord.inputs_provenance: dict[str, str]`; every context key labelled `"user_input"`, `"stage:{id}"`, `"memory"`, or `"stale_memory"`; persisted to SQLite |
| Drift score | ✅ Done | `ImprovementReport.drift_score`; `_load_all_verified_fixes()` reads full JSONL history; `score = len(regressed) / max(len(current), 1)` |
| Post-condition verification | ✅ Done | `ToolDescriptor.postcondition: Callable`; `PostconditionFailed` exception; `POSTCONDITION_FAILED` diagnostic code; engine checks after every tool dispatch |
| `fan_in: "consensus"` | ✅ Done | Parallel subagent results forwarded to `_consensus_judge()` (litellm); synthesizes conflicting outputs into single best answer |
| Component governance | ✅ Done | `_classify_changes()` auto-applies safe changes; writes `{spec}.pending.yaml` for structural changes; `ImprovementReport.requires_review` + `pending_path` |
| Rich dashboard (`armature dashboard`) | ✅ Done | 4-panel Rich terminal dashboard: health strip + sparkline, stage breakdown table, improvement timeline, safety & governance audit; `--watch`, `--format json`, `--last N` |

### CLI Commands

```bash
armature run <spec>              # execute a workflow from YAML
armature validate <spec>         # validate spec without running
armature new [output]            # interactive spec creation wizard
armature serve                   # start HTTP service
armature optimize <spec>         # meta-harness optimizer (single proposal)
armature report --run-id <id>    # per-run text report with failure signatures
armature dashboard <spec>        # Rich 4-panel aggregate health dashboard (multi-run)
armature dashboard <spec> --watch            # auto-refresh every 5 seconds
armature dashboard <spec> --format json      # machine-readable JSON output
armature export-traces           # export traces as SFT/DPO training data
armature improve <spec>          # analyze traces, propose and apply spec improvements
armature improve <spec> --apply-pending      # apply a staged pending.yaml revision
```

---

## How to Use This Harness When Building Agentic Teams

### The Mental Model: Teams Have Roles

Every effective agentic team follows the same four-role structure Armature enforces:

- A **worker** (or several) does the actual labor — processing data, generating content, writing code, making API calls. Workers are fast, cheap, and guided by strict output schemas.
- An **orchestrator** manages the overall task — decides what to do next, routes to the right worker, handles exceptions, maintains the big picture.
- A **judge** validates quality — checks that worker outputs actually solve the problem before the workflow advances. Without a judge, bad outputs propagate silently.
- A **researcher** enriches context — retrieves relevant information from external sources (documentation, databases, prior work) to improve worker and orchestrator performance.

Most simple workflows need only workers and a judge. Complex workflows add an orchestrator. Knowledge-intensive workflows add a researcher.

### Step 1: Define the Workflow as a YAML Spec

A customer support triage team might look like:

```
[researcher: retrieve customer history]
         ↓
[worker: classify intent and urgency]
         ↓
[judge: validate classification quality]
         ↓  (if quality < threshold, retry with enriched context)
[orchestrator: decide routing — escalate, auto-reply, or queue]
         ↓
[human gate: approve if escalating to senior support]
         ↓
[worker: draft response or ticket]
         ↓
[refiner (post_run): review transcript, propose improvements]
```

The `post_run` refiner is optional but recommended for any workflow you plan to run repeatedly. It closes the inner improvement loop automatically.

### Step 2: Assign Models to Tiers, Not to Stages

```yaml
model_tiers:
  small:    { provider: ollama, model: qwen2.5:7b }     # local, near-zero cost
  medium:   { provider: openrouter, model: qwen3.5-72b } # fast, moderate cost
  frontier: { provider: anthropic, model: claude-opus-4-7 } # best quality
```

Workers default to `small`. Judges and orchestrators use `frontier`. The harness escalates automatically when quality falls short.

### Step 3: Add Quality Gates — Don't Skip Them

Every workflow should have at least one judge stage. The judge uses RaaS deliberation to score output quality and block advancement if confidence is below threshold.

### Step 4: Wire Safety Rules Early

```yaml
safety_rules:
  - tool: shell_exec
    condition: { field: cmd, op: matches_regex, value: "rm\\s+-rf" }
    action: block
    message: "Destructive rm -rf blocked"
```

### Step 5: Wire Observability From the Start

Every harness run automatically writes to the `TraceStore`. After a handful of runs:

```bash
armature report --run-id <id>    # human-readable; shows IHR + failure signatures
```

IHR is a 0–1 composite: 40% output validity, 30% stage success rate, 20% RaaS score, 10% latency score. A well-tuned workflow should score above 0.80.

### Step 6: Run the Self-Improvement Loop

Once you have 3+ runs of a workflow:

```bash
# Analyze traces, propose a targeted spec revision, auto-apply
armature improve my-workflow.yaml

# Review what changed — the improvement log tracks every cycle
cat my-workflow.improve_log.jsonl | jq .

# To preview only (don't apply)
armature improve my-workflow.yaml --no-apply
```

The runner computes rolling IHR across your last 200 traces. If IHR is below target (default: 0.90), it calls `DiagnosticAnalyzer` to identify the dominant failure signature, then invokes a frontier LLM (`SpecRefiner`) to produce a targeted YAML revision — enriching stage descriptions, relaxing output schemas, or increasing retry limits based on the specific failure code. The revised spec is auto-applied and the original is overwritten in place.

For a richer optimizer experience (multi-iteration with causal history):

```python
from armature.optimizer.runner import OptimizerRunner

runner = OptimizerRunner(
    target_spec_path="workflows/my-workflow.yaml",
    trace_db_path="~/.armature/traces.db",
    proposal_db_path="~/.armature/optimizer/my-workflow.db",  # enables history
)
loop_result = await runner.run_loop(n_iterations=5)
```

### Step 7: Export Traces for SLM Fine-Tuning

When your judge tier runs at frontier quality, every accepted output is a training example. Export them:

```bash
# SFT training data (OpenAI ChatML / Qwen compatible)
armature export-traces \
  --workflow my-workflow \
  --output training/my-workflow.jsonl \
  --format chat \
  --min-score 0.85 \
  --role-types judge,researcher

# DPO preference pairs (chosen = high quality, rejected = low quality)
armature export-traces \
  --workflow my-workflow \
  --output training/my-workflow-dpo.jsonl \
  --format dpo \
  --min-score 0.85 \
  --rejected-max-score 0.30
```

The pipeline: frontier Opus runs as judge → high-quality traces accumulate → `export-traces` packages them as SFT/DPO data → fine-tune a small Qwen model → register as a new `small` model tier in the spec. The same workflow now runs at a fraction of the cost.

---

## Where We Stand Against the Research

| Paper Concept | Armature Coverage | Status |
|---|---|---|
| **Paper 1 — NLAH** | | |
| NLAH 7-component spec format | Fully implemented | ✅ |
| Four role types with model routing | Fully implemented | ✅ |
| `on_fail` recovery loops | Fully implemented | ✅ |
| IHR quality metric | `compute_ihr` + rolling IHR in `SelfImproveRunner` | ✅ |
| Trace collection | Structured, queryable `TraceStore` | ✅ |
| Parallel fan-out / fan-in | `fan_out`, `fan_in`, `partition_key` | ✅ |
| Cross-stage typed signatures | Spec-parse-time enforcement | ✅ |
| **Paper 2 — Meta-Harness** | | |
| Outer optimization loop (single-shot) | `OptimizerRunner.optimize()` | ✅ |
| A/B spec testing | IHR-based comparison | ✅ |
| Metric-driven optimization | Caller-supplied `metric_fn` | ✅ |
| Multi-iteration optimizer with history | `run_loop()` + `ProposalStore` | ✅ |
| Prompt bootstrapping from traces | `PromptBootstrapper` | ✅ |
| **Paper 3 — AutoHarness** | | |
| NL-to-spec synthesis | `SpecDrafter` | ✅ |
| Synthesis loop (generate → run → refine) | `AutoHarness` | ✅ |
| Harness-as-verifier (judge role) | Judge tier + RaaS | ✅ |
| **Paper 4 — AgentSpec** | | |
| Pre/post-tool hooks | `HookRegistry` | ✅ |
| Declarative safety rule DSL | `ToolSafetyRule` + `SafetyCondition` (6 operators) | ✅ |
| `ToolBlocked` non-retryable exception | Distinct from stage failures | ✅ |
| **Paper 5 — Continual Harness** | | |
| Failure signature taxonomy | `DiagnosticAnalyzer` (4 codes) | ✅ |
| Reset-free persistent memory (default) | `MemoryStore` cross-run load | ✅ |
| `fresh` memory override | `MemoryConfig.fresh: bool` | ✅ |
| In-run refiner stage (inner loop) | `Stage.post_run: bool` | ✅ |
| Cross-run self-improvement (outer loop) | `SelfImproveRunner` | ✅ |
| Targeted spec revision via LLM | `SpecRefiner` (returns HarnessSpec + raw YAML) | ✅ |
| Fine-tuning bridge | `TraceExporter` (SFT/DPO JSONL) | ✅ |
| Improvement audit trail | JSONL log per analysis cycle | ✅ |
| Failure signatures in run reports | `ReportBuilder` diagnostics section | ✅ |
| **Paper 6 — AHE** | | |
| Falsifiable improvement contract | `predicted_fixes` / `predicted_regressions` in `RefinerResult` | ✅ |
| Prediction verification across cycles | `_verify_predictions()` → `verified_fixes`, `missed_predictions`, `unexpected_regressions` | ✅ |
| Prior diagnostic state in audit log | `diagnostics_keys` field enables cross-cycle arithmetic | ✅ |
| Accountability tracking on `ImprovementReport` | Five new fields; full history in JSONL log | ✅ |
| **AGT — Microsoft Agent Governance Toolkit** | | |
| Tool reversibility classification | `Reversibility` enum; `_tool_reversibility` pseudo-field in safety rule conditions | ✅ |
| Trace arguments hash | `inputs_hash` SHA-256 in `TraceRecord`; auto-migrated on first `init()` | ✅ |
| Trace policy version | `policy_version` SHA-256 of `safety_rules` in `TraceRecord` | ✅ |
| Human approval gate in tool-call path | `require_approval` action on `ToolSafetyRule`; prompts operator for y/N | ✅ |
| Fail-closed strict mode | `safety_mode: strict` on `HarnessSpec`; `action: allow` for explicit whitelisting | ✅ |
| **Paper 7 — From Model Scaling to System Scaling** | | |
| Memory staleness (ℳ failure) | `staleness_threshold_days`; `_stale_memory_keys` context injection | ✅ |
| Context provenance (𝒞 failure) | `inputs_provenance` on `TraceRecord`; per-key origin labels | ✅ |
| Drift score (𝒪 accountability) | `drift_score` on `ImprovementReport`; cross-cycle regression metric | ✅ |
| Post-condition verification (𝒮 failure) | `ToolDescriptor.postcondition`; `PostconditionFailed`; `POSTCONDITION_FAILED` diagnostic | ✅ |
| Consensus fan-in (𝒮) | `fan_in: "consensus"`; `_consensus_judge()` litellm call | ✅ |
| Component governance (𝒢) | `_classify_changes()`; auto vs. review classification; `pending.yaml` staging | ✅ |

**All research and industry-framework gaps are closed.** The next capability horizon is model-tier registration automation (auto-registering a LoRA-fine-tuned Qwen checkpoint as a model tier after export) and a visual workflow editor.

---

## Technical Notes for Integration

### Python Library

```python
from armature import Harness

harness = Harness.from_spec("workflows/my-workflow.yaml")
result = await harness.run({"customer_id": "cust_123", "issue": "billing question"})
```

### HTTP Service (for non-Python consumers)

```bash
armature serve --host 0.0.0.0 --port 8080
```

**Synchronous** (simple use cases, server-side batch jobs):
```http
POST /run
Content-Type: application/json
{ "spec_path": "workflows/my-workflow.yml", "inputs": { "customer_id": "cust_123" } }

→ { "run_id": "a3f7b2d1", "status": "complete", "result": { ... } }
```

**Asynchronous** (production chatbots, long-running workflows):
```http
POST /run/async
→ 202 { "job_id": "abc12345", "status": "pending" }

GET /run/abc12345
→ { "job_id": "abc12345", "status": "complete", "result": { ... } }

GET /run/abc12345/events    (SSE stream)
→ data: {"type": "stage_start",    "stage_id": "gather"}
→ data: {"type": "stage_complete", "stage_id": "gather"}
→ data: {"type": "run_complete",   "run_id": "abc12345"}
```

See `docs/INTEGRATION.md` for the full LangGraph sidecar pattern including the latency acknowledgement token and Docker Compose template.

### Environment Variables

```bash
ANTHROPIC_API_KEY=...          # for frontier model calls
OPENROUTER_API_KEY=...         # for medium-tier calls
ARMATURE_SPECS_DIR=./workflows # default spec location
ARMATURE_RUNS_DIR=~/.armature/runs
OTEL_EXPORTER_OTLP_ENDPOINT=... # optional: send traces to Jaeger/Grafana
```

---

## Summary

Armature is a production-grade agent harness synthesized from seven academic papers spanning February–May 2026, plus five governance concepts borrowed from Microsoft's Agent Governance Toolkit. It handles the structural engineering — orchestration, quality control, failure recovery, observability, safety enforcement, and self-improvement — so that every team building on top of it can focus on the domain problem rather than the execution infrastructure.

The sixth paper, AHE (arXiv:2604.25850), added accountability to the improvement loop: every spec revision now carries a falsifiable contract, and each subsequent run verifies whether the predicted fixes actually materialized. The harness does not just improve — it explains itself as it does, cycle by cycle.

The seventh paper (arXiv:2605.26112) addressed three system-level failure modes the earlier papers left open: stale memory reaching LLMs without warning, context values flowing between stages without provenance, and tool side effects going unverified. It also introduced two architectural additions — a drift score that catches regressions across improvement cycles before they compound, and component governance that separates safe auto-applied changes from structural changes that warrant human review before deployment. The `fan_in: "consensus"` strategy closes the last gap: parallel subagent disagreements are now resolved by a dedicated judge model rather than silently discarded.

The AGT governance layer added five capabilities the academic papers left open: a principled reversibility classification for every tool call, tamper-evident hashing of trace inputs and the governing policy, a human approval gate wired directly into the tool-call path, and a strict fail-closed mode for deployments where the default must be deny rather than allow. These are production governance primitives, not academic proposals — borrowed from an industry team that has been running agentic systems in regulated environments.

The async HTTP service and LangGraph sidecar pattern complete the integration story: Armature is clearly positioned as a **batch-oriented multi-stage work engine**, not a conversational loop. LangGraph owns the conversation; Armature owns the heavy lifting inside each turn. The SSE event stream and latency acknowledgement pattern let chatbot users see progress immediately while multi-stage work runs in the background.

With all seven papers and the AGT framework implemented, the harness now has:
- **Execution**: DAG orchestration, four role types, parallel fan-out (including consensus synthesis), guided JSON output, model-tier auto-escalation
- **Quality**: IHR metric, RaaS deliberation, output schema validation, declarative evaluation stages, post-condition verification
- **Safety**: fail-closed strict mode, five rule actions including human approval, reversibility-based blocking, `ToolBlocked` non-retryable exception
- **Observability**: tamper-evident trace records with inputs hash, policy version, and per-key provenance; OpenTelemetry; run reports with failure signatures; drift score for regression detection
- **Memory**: cross-run persistence, staleness detection, `_stale_memory_keys` warnings, knowledge extraction
- **Self-improvement**: inner refiner loop, outer `SelfImproveRunner`, prediction-verification accounting, component governance, SFT/DPO trace export

1,198 tests. All research and industry-framework gaps closed.

---

*For implementation details, see `VISION.md`. For integration patterns, see `docs/INTEGRATION.md`. For the full test suite, see `tests/`.*
