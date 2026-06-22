# Armature: Philosophy, Research Foundation, and Architecture

**Audience:** Engineering leadership  
**Date:** 2026-05-26  
**Author:** Bryan Sparks  
**Status:** Active development — All phases complete (1,388 tests passing)

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

Armature synthesizes eleven academic papers published between February 2025 and June 2026, plus one industry governance framework released in 2025, plus one open-source agent architecture project — all converging on the same insight: **the harness is more important than the model.**

### Paper 1: Natural-Language Agent Harnesses (NLAH)
**Tsinghua University, March 2026** — [arXiv:2603.25723](https://arxiv.org/abs/2603.25723)

The paper that defines the architectural model. NLAH establishes that workflows defined in structured natural language outperform equivalent code-based harnesses on complex benchmark tasks (47.2% vs. 30.4% on OSWorld). The key finding: when the harness is readable and editable text, the entire system — including an optimizer — can reason about it.

NLAH specifies seven harness components: Contracts, Roles, Stages, Adapters, State, Failure Taxonomy, and File-backed State. Armature implements all seven. The paper also defines HQS (Harness Quality Score), a composite quality metric used to score run quality objectively.

NLAH also specifies parallel fan-out as a core orchestration primitive — dispatching N child harnesses simultaneously then recombining their outputs — which is fully implemented in Armature.

**Armature contributions from this paper:** YAML spec format, four role types, DAG executor, HQS computation, parallel fan-out/fan-in with configurable partition and merge strategies.

### Paper 2: Meta-Harness — Automated Optimization End-to-End
**Stanford University, March 2026** — [arXiv:2603.28052](https://arxiv.org/abs/2603.28052)

The paper behind Armature's optimizer. Meta-Harness introduces an outer optimization loop where a frontier model reads execution traces from prior runs and proposes improvements to the harness spec itself. The critical finding: giving the optimizer access to full execution traces (not just pass/fail scores) improves accuracy from 41% to 57% on benchmark tasks. The optimizer reasons causally about why runs succeeded or failed and proposes targeted edits.

The paper's key insight on multi-iteration optimization: giving the optimizer access to the full history of prior proposals — what was tried, whether it was accepted, and what the score was — enables causal reasoning that avoids re-proposing failed changes and compounds on successful ones.

**Armature contributions:** `OptimizerRunner` (3-stage: analyze → propose → evaluate), A/B spec testing by HQS comparison, `ProposalStore` (SQLite-backed proposal history), `run_loop()` for N-iteration optimization with causal reasoning.

### Paper 3: AutoHarness — LLM-Synthesized Harnesses
**February 2026** — [arXiv:2603.03329](https://arxiv.org/abs/2603.03329)

AutoHarness demonstrates that LLMs can write their own harness code through iterative refinement — producing harnesses that outperform larger models without harnesses. The insight most applicable to Armature: the concept of a **harness-as-verifier**, where the harness validates that agent outputs meet domain-specific legality constraints before accepting them. This is the conceptual ancestor of Armature's judge role. The paper also introduced the synthesis loop: given a natural-language task description, generate a spec, run it, evaluate it, and refine — iterating until quality passes a threshold.

**Armature contributions:** `SpecDrafter` (NL → YAML spec synthesis), `AutoHarness` synthesis loop (generate → run → evaluate → refine, up to N iterations), `PromptBootstrapper` (inject high-quality trace examples as few-shot prompts into spec descriptions).

### Paper 4: AgentSpec — Runtime Enforcement for Safe Agents
**March 2025** — [arXiv:2503.18666](https://arxiv.org/abs/2503.18666)

The safety paper. AgentSpec introduces a declarative rule language for constraining agent behavior at runtime: "before this tool call, if this condition is true, stop/ask/correct." The rules are composable, lightweight (sub-millisecond evaluation), and can themselves be generated by LLMs. Armature implements the full AgentSpec enforcement architecture: pre/post-tool hooks wired into the engine, and a declarative condition DSL (`ToolSafetyRule` + `SafetyCondition`) that workflow authors write directly in YAML.

**Armature contributions:** `HookRegistry` (pre/post-stage and pre/post-tool), `ToolSafetyRule` + `SafetyCondition` declarative YAML DSL (6 operators: `contains`, `not_contains`, `equals`, `not_equals`, `matches_regex`, `truthy`; actions: `block`, `warn`, `log`), `ToolBlocked` non-retryable exception type. *Extended by AGT concepts — see below.*

### Paper 5: Continual Harness — Reset-Free Self-Improvement for Agentic Systems
**May 2026** — [arXiv:2605.09998](https://arxiv.org/abs/2605.09998)

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
- `SelfImproveRunner` — the outer self-improvement loop: load traces → compute rolling HQS → run `DiagnosticAnalyzer` → call `SpecRefiner` (frontier LLM rewrites targeted YAML sections) → auto-apply revised spec → write audit log (`armature improve <spec>` CLI command)
- `TraceExporter` — the fine-tuning bridge: export high-quality traces (by quorum score) as SFT training data (chat/alpaca/sharegpt formats) or DPO preference pairs (chosen/rejected matched by stage) for LoRA fine-tuning smaller models (`armature export-traces` CLI command)
- Failure signatures section in `armature report` output — makes diagnostic analysis human-readable

### Paper 6: Agentic Harness Engineering (AHE) — Observability-Driven Automatic Evolution
**April 2026** — [arXiv:2604.25850](https://arxiv.org/abs/2604.25850)

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
**May 2026** — [arXiv:2605.26112](https://arxiv.org/abs/2605.26112)

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

### Paper 8: Harness Updating Is Not Harness Benefit — Efficient Spec Evolution
**May 2026** — [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1

The paper that answered the most practical question about the self-improvement loop: does the model proposing spec changes need to be as powerful as the model executing them? The answer is no — and the margin is surprisingly small. Across benchmarks, medium-tier evolvers achieved within 3.1 percentage points of frontier-tier evolvers at substantially lower cost. This validated using a cheaper model for `SpecRefiner` without sacrificing improvement quality.

The paper also introduced two new harness quality metrics that fill gaps in the original HQS formula:

**Skill-Load Rate (SLR)** measures whether declared tools and skills are actually invoked during execution. A stage that declares `web_search` in its role configuration but never calls it has a role description that fails to prompt tool use — a detectable and fixable problem. SLR = 0 on a stage that always no-ops its declared skills is a distinct failure mode from `output_invalid` or `stage_failed`, requiring a specific fix (rewrite the description to explicitly name the tool and the condition under which it should be called).

**Harness-Following Rate (HFR)** measures whether models adhere to harness instructions on the first attempt. A stage that always hits `on_fail.loop` before succeeding still counts as a success — but it reveals that the base-tier model needed extra prodding, which is a quality signal worth tracking. HFR is the fraction of stage executions that required zero escalations.

Both metrics are now components of HQS, updating the formula from a 4-component weighted sum to a 5-component sum.

**Armature contributions from this paper:**
- `SpecRefiner` explicitly uses a medium-tier model by default; docstring updated to reflect the intentional cost-quality trade-off
- `low_skill_activation` added to `DiagnosticCode` — fires when a stage's `TraceRecord.tools_declared` is non-empty but `tools_called` is empty across a run; `SpecRefiner` system prompt updated to advise rewriting the description to prompt tool use
- `TraceRecord.tools_declared` and `tools_called` fields — populated by `LLMNode` from the stage's declared tools and the tool names actually invoked during the ReAct loop
- HFR (Harness-Following Rate) added as the fifth HQS component; weights updated from `0.40/0.30/0.20/0.10` to `0.35/0.25/0.20/0.10/0.10` (output_valid / success / quorum / latency / hfr); both `TraceStore.compute_hqs` and `SelfImproveRunner._compute_hqs` updated

### Paper 9: Self-Harness — Safer and Smarter Spec Evolution
**June 2026** — [arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1

The paper that made the improvement loop safer and smarter. Where the prior papers established *that* the harness should improve and *when* it should trigger, Self-Harness addresses *how* to make proposals that are more targeted, more diverse, and less likely to break things that are already working.

Self-Harness introduces a 3-stage loop: Weakness Mining (characterize failures in depth), Harness Proposal (generate candidates), and Proposal Validation (gate on held-in/held-out split). Armature adopted four mechanisms from this paper:

**1. Causal 3-tuple failure attribution.** Rather than a flat failure code, each diagnostic carries a triple `φ(r) = (terminal_cause, causal_status, mechanism)`. Terminal cause is *what* broke. Causal status is *whose fault* it is — spec problem, model problem, or tool problem. Mechanism is *how*: timeout, underpowered model, schema too strict, missing instruction. The flat code `stage_failed` is the same for a timeout and a runtime error; the triple tells the refiner they need completely different fixes.

**2. Declared editable surfaces.** The spec declares which surfaces the refiner may touch via `self_improvement.editable_surfaces`. Surfaces not listed are named in the refiner's system prompt as explicitly locked. This bounds the refiner's search space and prevents it from hallucinating structural changes (adding stages, modifying safety rules) in response to symptoms that don't require them.

**3. K-proposal diversity with best-coverage selection.** Instead of generating one proposal, the runner generates K candidates in parallel, each steered by a different diversity hint (minimize changes / fix output format / adjust model tier / tighten schema). The candidate whose `predicted_fixes` most overlap the active diagnostic codes is selected. Ensemble generation consistently finds proposals that address more failure modes than single-shot refinement.

**4. Held-out trace-split regression gating.** Self-Harness tests proposed harnesses on a held-out benchmark split. Armature adapts this to the trace-based setting: stages with no current diagnostics are treated as the held-out set — proposals that modify those healthy stages are flagged as regression risks and filtered before selection. If all candidates are risky, the best of the risky set is used as a fallback.

**Armature contributions from this paper:**
- `CausalAttribution` model (`terminal_cause`, `causal_status`, `mechanism`) added to `DiagnosticResult`; all six diagnostic codes now emit a typed 3-tuple
- `EditableSurface` enum + `SelfImprovementConfig` + `HarnessSpec.self_improvement` field — declarative surface locking at the spec level
- `_make_refiner_system_prompt(editable_surfaces, diversity_hint)` — dynamic prompt construction; names locked surfaces explicitly
- `SpecRefiner.refine_many(n_proposals, ...)` — parallel candidate generation via `asyncio.gather` with rotating diversity hints
- `_pick_best_proposal(candidates, diagnostics)` — selects the candidate whose `predicted_fixes` most overlap active diagnostic codes
- `_healthy_stage_ids(traces, diagnostics)` + `_proposal_regression_risk(candidate, old_spec, healthy_stage_ids)` — regression gating before selection
- `ImprovementReport.n_proposals_generated` + `regression_risk_count` — audit fields written to `ImprovementReport` and JSONL log

### Paper 10: Skill-to-LoRA — From Using Skills to Learning Behaviors for Token-Efficient LLM Agents
**The Chinese University of Hong Kong, June 2026** — [arXiv:2606.16769](https://arxiv.org/abs/2606.16769)

The paper behind adapter-backed skills. S2L starts from the observation that agent skills are commonly distributed as SKILL.md procedural documents that are injected into prompts in full at runtime. That approach is modular and human-readable, but it consumes context window and repeats the same instructions on every call. S2L replaces the inline skill text with a small LoRA adapter trained to reproduce the skill-induced behavior. The full document is used offline to generate demonstrations; the adapter is plugged in online to invoke the learned behavior.

Armature's implementation matches the paper directly:

- **Skill-to-LoRA (`s2l`) backend** — trains a LoRA adapter from a skill document so the skill's behavior is captured in weight space.
- **Trace backend** — trains adapters from exported high-quality traces, the natural continuation of the fine-tuning bridge described in Continual Harness.
- **`skill_library.adapter` references** — declare which adapter a skill should load, with `version: latest` resolving the promoted pointer.
- **`adapter_support: dynamic` tiers** — the engine passes the resolved adapter artifact to the provider in provider-specific kwargs and omits the original skill text from the prompt, cutting prefill tokens while preserving behavior.
- **Fallback policies** — when an adapter is unavailable, the harness can fall back to the skill text, omit the skill, or fail explicitly.

**Armature contributions from this paper:**
- `AdapterFactory` ABC with `submit` / `poll` / `available` methods
- `AdapterRegistry` for versioned local storage with `manifest.json`
- `skill_library` entries with optional `adapter` block
- `model_tiers.*.adapter_support` (`dynamic` | `none`) and `adapter_path_template`
- `MergedAdapterFactory` for parameter-space merging of registered adapters
- CLI: `armature adapter create/promote/merge/eval`

### Paper 11: C-LoRA — Continual Low-Rank Adaptation for Pre-trained Models
**Shanxi University / University of Manchester, February 2025** — [arXiv:2502.17920](https://arxiv.org/abs/2502.17920)

C-LoRA addresses a problem that appears immediately when adapter-backed skills are deployed in production: skills and trace batches arrive sequentially, but standard LoRA is trained once per dataset. The existing choices are either to keep a separate adapter per version (parameter growth, inference-time routing) or to retrain from scratch each time (catastrophic forgetting). C-LoRA proposes a single-adapter continual-learning approach: keep shared low-rank matrices `A` and `B`, and insert a learnable routing matrix `R` between them so that

```
W_t = W_0 + A · R · B
```

`R` is split into a frozen `R_old` (preserving prior-task knowledge) and a trainable near-zero `R_delta` for the new task. New updates are regularized away from old subspaces via `L = L_ce + λ ||A^T · R_delta||_F²` with `λ = 0.01`. The result is a single adapter that accumulates knowledge across sequential skill/trace updates without the memory or routing overhead of multiple adapters.

This maps directly to Armature's adapter registry, where each skill already versions adapters. C-LoRA turns version `N+1` into a continual update from version `N` rather than a fresh training run.

**Armature contributions from this paper:**
- `AdapterFactoryConfig.use_dora` — Weight-Decomposed Low-Rank Adaptation (DoRA) option for richer adapter representations.
- `AdapterFactoryConfig.continual_learning` — `ContinualLearningConfig` with `enabled`, `prior_version`, `orthogonality_lambda`, `freeze_old_routing`, and `init_delta_near_zero`.
- `AdapterRequest.use_dora`, `continual_learning`, and `prior_adapter_version` — propagated through every backend (mock, s2l, trace, local, remote, merge) and into `AdapterMetadata`.
- Trainer stubs persist `use_dora`, `continual_learning`, and `prior_adapter_version` in `adapter_config.json` so production trainer implementations can read the flags and execute the actual C-LoRA math.

---

## Architecture Overview

### The Nine Core Components

Every production harness must implement all nine of these. Armature ships them all.

| # | Component | What It Does | Armature Implementation |
|---|-----------|-------------|------------------------|
| 1 | **Iteration Loop** | The core while-loop: assemble context → call model → dispatch tool → update state → check termination | `DAGExecutor` + async stage runner |
| 2 | **Context Management** | Decides what to keep, summarize, or drop as context grows | `ContextManager`, prompt assembler pipeline |
| 3 | **Tools, Skills, Registry** | Primitives (file I/O, HTTP, shell) and higher-level skills (SLM/LoRA, RAG, Quorum (another open source project to consider)) | `ToolRegistry`, `BuiltinSkills` |
| 4 | **Subagents** | Fan out work to N child agents in parallel with isolated context and focused roles | `SubagentNode`, `asyncio.gather`, `fan_out`/`fan_in`/`partition_key` |
| 5 | **Built-in Skills** | Non-negotiable out-of-box capabilities: retrieval, deliberation, trace submission | SLM/LoRA, RAG, Quorum (another open source project to consider) skills bundled |
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
| `judge` | Frontier | Quality scoring, evaluation, consensus deliberation. The only role that can block a workflow from advancing. |
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
   │  — sees _transcript   │          │  2. Compute rolling HQS      │
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
                │  Loop 4: Quorum           │
                │  Calibrate deliberation   │
                │  priors from outcomes →   │
                │  cleaner quality signal   │
                └───────────────────────────┘
```

**The key property:** the harness gets measurably better the more it runs — without any human engineering effort after initial deployment. Traces fuel fine-tuning. Fine-tuned models produce better traces. Better traces improve both the spec optimizer (outer loop) and the in-run refiner (inner loop). Better specs produce better traces.

---

## Current Implementation State

All planned phases are complete. 1,388 tests, passing.

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
| HQS computation | ✅ Done | 4-component quality metric: output validity, success rate, quorum, latency |
| Harness optimizer workflow | ✅ Done | 3-stage: analyze → propose → evaluate |
| A/B spec testing | ✅ Done | HQS-based comparison |
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
| `armature report` command | ✅ Done | Human-readable run report: HQS, stage results, failure signatures |
| `DiagnosticAnalyzer` | ✅ Done | 4-code failure signature taxonomy extracted from trace records |
| `MemoryConfig.fresh` | ✅ Done | Skip loading prior memories for a clean-slate run |
| `Stage.post_run` | ✅ Done | In-run refiner stage: receives `_transcript` + `_diagnostics` after DAG completes |
| `TraceExporter` | ✅ Done | Export traces as SFT (chat/alpaca/sharegpt) or DPO pairs; `armature export-traces` |
| `SelfImproveRunner` | ✅ Done | Outer self-improvement loop; auto-applies revised spec; `armature improve` |
| Improvement audit log | ✅ Done | JSONL log of every analysis cycle (HQS, diagnostics, applied flag) |
| Prediction-verification loop | ✅ Done | `RefinerResult` carries `predicted_fixes`/`predicted_regressions`; each cycle verifies prior predictions against observed diagnostic shift |
| Memory staleness detection | ✅ Done | `MemoryStore.staleness_threshold_days` (default 30 days); stale entries surface as `set[tuple]`; `_stale_memory_keys` injected into run context |
| Context provenance tracking | ✅ Done | `TraceRecord.inputs_provenance: dict[str, str]`; every context key labelled `"user_input"`, `"stage:{id}"`, `"memory"`, or `"stale_memory"`; persisted to SQLite |
| Drift score | ✅ Done | `ImprovementReport.drift_score`; `_load_all_verified_fixes()` reads full JSONL history; `score = len(regressed) / max(len(current), 1)` |
| Post-condition verification | ✅ Done | `ToolDescriptor.postcondition: Callable`; `PostconditionFailed` exception; `POSTCONDITION_FAILED` diagnostic code; engine checks after every tool dispatch |
| `fan_in: "consensus"` | ✅ Done | Parallel subagent results forwarded to `_consensus_judge()` (litellm); synthesizes conflicting outputs into single best answer |
| Component governance | ✅ Done | `_classify_changes()` auto-applies safe changes; writes `{spec}.pending.yaml` for structural changes; `ImprovementReport.requires_review` + `pending_path` |
| Rich dashboard (`armature dashboard`) | ✅ Done | 4-panel Rich terminal dashboard: health strip + sparkline, stage breakdown table, improvement timeline, safety & governance audit; `--watch`, `--format json`, `--last N` |
| LLM response caching | ✅ Done | `LLMCache` SQLite-backed, content-addressed by SHA-256(model + messages + output_mode); `Harness` creates at `~/.armature/llm_cache.sqlite`; `--no-cache` flag on `armature run` |
| Audit replay (`armature replay`) | ✅ Done | Reads TraceStore records for any `run_id`; displays Rich stage table + aggregate HQS; `--traces` flag for custom DB |
| Trace-triggered behaviors | ✅ Done | `BehaviorRule` / `BehaviorRegistry`; `make_default_behavior_registry()` pre-loads `hqs_feedback` (fires when HQS < 0.75); evaluated post-run inside `Harness.run()` |
| Auto self-improvement (`--auto-improve`) | ✅ Done | `armature run <spec> --auto-improve`: after every run, checks rolling HQS against 0.75 threshold; if below, calls `SelfImproveRunner.analyze()` and auto-applies safe revisions or stages structural changes to `{spec}.pending.yaml` for review |
| Static spec risk score (KYA-inspired) | ✅ Done | `armature validate` now computes a [0–100] risk score per spec: +4/tool-call stage, +15/no judge, +8/require_approval rule, +6/fan-out stage, −10/strict mode; tier LOW/MEDIUM/HIGH/CRITICAL surfaced with factor breakdown |
| Rogue signal counter (KYA-inspired) | ✅ Done | `RogueSignalCounter` passed to `SafetyHookBuilder`; incremented on every `ToolBlocked` or `require_approval` refusal; `run_summary` event includes `rogue_signals` count; CLI prints `N blocked` when non-zero |
| Only-tighten safety rule validation (KYA-inspired) | ✅ Done | `validate_spec()` now flags `CONFLICTING_SAFETY_RULES` when an `allow` rule targets the same tool as a `block` rule (or wildcard block) — enforces the only-tighten composition principle |

### CLI Commands

```bash
armature run <spec>                  # execute a workflow from YAML
armature run <spec> --no-cache       # run without LLM response cache
armature run <spec> --auto-improve   # run then auto-apply spec improvements if HQS < 0.75
armature validate <spec>             # validate spec without running
armature new [output]                # interactive spec creation wizard
armature serve                       # start HTTP service
armature optimize <spec>             # meta-harness optimizer (single proposal)
armature report --run-id <id>        # per-run text report with failure signatures
armature replay <run_id>             # display a historical run stage-by-stage from TraceStore
armature dashboard <spec>            # Rich 4-panel aggregate health dashboard (multi-run)
armature dashboard <spec> --watch    # auto-refresh every 5 seconds
armature dashboard <spec> --format json  # machine-readable JSON output
armature export-traces               # export traces as SFT/DPO training data
armature improve <spec>              # analyze traces, propose and apply spec improvements
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

Every workflow should have at least one judge stage. The judge uses consensus deliberation to score output quality and block advancement if confidence is below threshold. (Quorum is another open source project to consider for structured multi-model deliberation.)

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
armature report --run-id <id>    # human-readable; shows HQS + failure signatures
```

HQS is a 0–1 composite: 40% output validity, 30% stage success rate, 20% quorum score, 10% latency score. A well-tuned workflow should score above 0.80.

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

The runner computes rolling HQS across your last 200 traces. If HQS is below target (default: 0.90), it calls `DiagnosticAnalyzer` to identify the dominant failure signature, then invokes a frontier LLM (`SpecRefiner`) to produce a targeted YAML revision — enriching stage descriptions, relaxing output schemas, or increasing retry limits based on the specific failure code. The revised spec is auto-applied and the original is overwritten in place.

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
| HQS quality metric | `compute_hqs` + rolling HQS in `SelfImproveRunner` | ✅ |
| Trace collection | Structured, queryable `TraceStore` | ✅ |
| Parallel fan-out / fan-in | `fan_out`, `fan_in`, `partition_key` | ✅ |
| Cross-stage typed signatures | Spec-parse-time enforcement | ✅ |
| **Paper 2 — Meta-Harness** | | |
| Outer optimization loop (single-shot) | `OptimizerRunner.optimize()` | ✅ |
| A/B spec testing | HQS-based comparison | ✅ |
| Metric-driven optimization | Caller-supplied `metric_fn` | ✅ |
| Multi-iteration optimizer with history | `run_loop()` + `ProposalStore` | ✅ |
| Prompt bootstrapping from traces | `PromptBootstrapper` | ✅ |
| **Paper 3 — AutoHarness** | | |
| NL-to-spec synthesis | `SpecDrafter` | ✅ |
| Synthesis loop (generate → run → refine) | `AutoHarness` | ✅ |
| Harness-as-verifier (judge role) | Judge tier + consensus deliberation | ✅ |
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
| **ActiveGraph — Event-Sourced Execution ([arXiv:2605.21997](https://arxiv.org/abs/2605.21997))** | | |
| LLM response caching | `LLMCache`; SHA-256 keyed; `--no-cache` flag | ✅ |
| Audit replay | `armature replay <run_id>`; reads `TraceStore.query_by_run()`; Rich stage table | ✅ |
| Trace-triggered behaviors | `BehaviorRule` / `BehaviorRegistry`; `hqs_feedback` built-in | ✅ |
| Auto self-improvement | `--auto-improve` on `armature run`; closes behavior → improve loop | ✅ |
| Fork-and-diff | Branch at historical stage; diff downstream outputs | 📋 Roadmap |
| True re-execution replay | Reconstruct + re-run from LLM cache | 📋 Roadmap |
| Full event sourcing | Append-only log as source of truth | 📋 Roadmap |
| **KYA — Know Your Agents ([arXiv:2605.25376](https://arxiv.org/abs/2605.25376))** | | |
| Static spec risk score | `compute_spec_risk()`; 5 factors; LOW/MEDIUM/HIGH/CRITICAL; surfaced by `armature validate` | ✅ |
| Rogue signal counter | `RogueSignalCounter`; incremented on every `ToolBlocked`; in `run_summary` | ✅ |
| Only-tighten safety rule validation | `CONFLICTING_SAFETY_RULES` error code in `validate_spec()` | ✅ |
| **Paper 9 — Self-Harness ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1)** | | |
| Causal 3-tuple failure attribution | `CausalAttribution` (terminal_cause / causal_status / mechanism) on every `DiagnosticResult` | ✅ |
| Declared editable surfaces | `HarnessSpec.self_improvement.editable_surfaces`; dynamic refiner prompt names locked surfaces | ✅ |
| K-proposal diversity + best-coverage selection | `SpecRefiner.refine_many()`; rotating diversity hints; `_pick_best_proposal()` | ✅ |
| Held-out trace-split regression gating | `_healthy_stage_ids()`; `_proposal_regression_risk()`; `regression_risk_count` on `ImprovementReport` | ✅ |

**All research and industry-framework gaps are closed.** KYA closes the final gap: definition-layer risk scoring and safety rule composition governance that operates before a workflow ever executes.

### Open-Source Project: ActiveGraph (Event-Sourced Agent Architecture)
**Yohei Nakajima, May 2026** — [arXiv:2605.21997](https://arxiv.org/abs/2605.21997) / [github.com/yoheinakajima/activegraph](https://github.com/yoheinakajima/activegraph)

ActiveGraph formalizes an event-sourced execution model for agent systems — where the append-only event log is the source of truth and agent state is a deterministic projection. Its most adoptable ideas: content-addressed LLM response caching (identical requests serve from a local SQLite cache), audit replay (re-display any historical run stage-by-stage from recorded traces), and trace-triggered behaviors (pattern-match on trace history to fire reactive handlers automatically).

Three of these concepts are now implemented in Armature. Two (fork-and-diff, full event sourcing) are deferred to a future architecture phase.

**Armature contributions from this project:**
- `LLMCache` (`armature/cache/llm_cache.py`) — content-addressed SQLite cache keyed by SHA-256 of (model, messages, output_mode); `Harness` creates one at `~/.armature/llm_cache.sqlite` by default; `--no-cache` flag on `armature run` disables it
- `armature replay <run_id>` — CLI command that reads all traces for a historical run via `TraceStore.query_by_run()`, displays a Rich stage-by-stage table, and prints the aggregate HQS; `--traces` flag for custom DB path
- `BehaviorRule` / `BehaviorRegistry` — reactive pattern-matching layer on top of trace history; a `BehaviorRule` carries a `pattern: Callable[[list[TraceRecord]], bool]` and a `handler`; `BehaviorRegistry.evaluate()` fires matching handlers after each run; built into `Harness.run()` post-run
- `_hqs_feedback_pattern` built-in behavior — fires when rolling HQS over recent traces falls below 0.75 and trace count ≥ 3; prints a suggestion to run `armature improve`; registered by default via `make_default_behavior_registry()`
- `--auto-improve` flag on `armature run` — closes the reactive loop: after each run, Armature checks rolling HQS; if below 0.75, calls `SelfImproveRunner.analyze()` automatically, applies safe revisions in-place, or writes structural proposals to `{spec}.pending.yaml` for human review before applying

### Open-Source Project: KYA — Know Your Agents (Trust Layer for Autonomous Systems)
**Kolawole Quadri (Veldt Labs), May 2026** — [arXiv:2605.25376](https://arxiv.org/abs/2605.25376) / `veldt-kya` on PyPI — Apache 2.0

KYA is a framework-agnostic governance layer for autonomous systems. Where AgentSpec and AGT address *runtime enforcement* (what an agent is allowed to do), KYA addresses *definition-layer risk* (how dangerous the agent configuration itself is) and *runtime trust drift* (whether behavior deviated from definition across a run). Its three most adoptable concepts: static risk scoring at load time, rogue signal counting during execution, and the only-tighten composition algebra for safety rule inheritance.

**Armature contributions from this project:**
- **Static spec risk score** (`armature/spec/risk.py`) — `compute_spec_risk(spec)` produces a `SpecRiskResult` with `score` [0–100], `tier` (LOW/MEDIUM/HIGH/CRITICAL), and `factors` list. Factors: +4/tool-call stage, +15/no judge stage, +8/require_approval rule, +6/fan-out stage, −10/strict safety mode. `armature validate` displays the score and factor breakdown immediately after confirming spec validity.
- **Rogue signal counter** (`RogueSignalCounter` in `armature/hooks/lifecycle.py`) — a lightweight counter passed into `SafetyHookBuilder.register(counter=...)` and incremented on every `ToolBlocked` raise (block action or denied approval). `Harness` creates one per run; the count appears in the `run_summary` event and CLI output (`N blocked`) when non-zero.
- **Only-tighten safety rule validation** — `validate_spec()` now emits `CONFLICTING_SAFETY_RULES` when an `allow` rule targets the same tool (or matches a wildcard `*`) as an existing `block` rule. Enforces KYA's only-tighten principle: safety rules may add restrictions, never loosen them.

### What Armature Improves Over Time

When `--auto-improve` is active (or `armature improve` is run manually after enough traces accumulate), the `SelfImproveRunner` + `SpecRefiner` loop makes targeted edits to the spec. These are the classes of change that compound over successive runs:

**Prompt quality → fewer parse errors**
Role descriptions that are vague or under-specified cause workers to produce outputs the output schema rejects. `SelfImproveRunner` surfaces `OUTPUT_INVALID` diagnostics on specific stage IDs and rewrites those role descriptions to add explicit formatting expectations, enumeration constraints, and output examples. After a cycle or two, the parse-failure rate on those stages drops measurably.

**Output schema relaxation → fewer false validation failures**
Overly strict `output_schema` definitions (required fields that are situationally absent, enum values that miss edge cases) produce false negatives — valid outputs rejected by the schema. The refiner loosens or corrects these without removing validation, reducing wasted retries while maintaining quality gates.

**Model tier rebalancing → better cost-quality trade-off**
Stages that repeatedly hit `HIGH_ESCALATION` (calling a higher-tier model via `on_fail.loop`) are automatically upgraded so the base tier matches the task difficulty. Stages that never escalate can be safely downgraded. Over time, the spec self-calibrates so each stage runs at the cheapest tier that reliably succeeds.

**Retry limit tuning → better failure recovery**
`STAGE_FAILED` diagnostics on a stage indicate the current `on_fail.loop.max` isn't high enough. The refiner increases it, adding retry budget where statistically needed. Stages that never use retries get trimmed to reduce unnecessary latency.

**Judge criteria enrichment → better quality discrimination**
When a judge stage's quorum scores cluster near 0.5 (effectively a coin flip), the `LOW_CONFIDENCE` diagnostic fires. The refiner adds explicit evaluation criteria to the judge's role description — specific rubrics, score anchors, tiebreaker logic — that force sharper, more consistent judgment. HQS's quorum component rises as a result.

The distinction between iteration and retry reflects a deeper design principle: the YAML spec should declare *intent*, not *mechanism*. `on_fail.loop` declares "this might fail, try again." `loop` declares "do this repeatedly until a condition is met." Conflating the two forces workflow authors to encode iteration intent in retry semantics — using undefined-on-first-attempt variables and inverted until conditions. Separate declarations make the spec self-documenting and eliminate the semantic gap between what the author means and what the runtime does.

**Structural changes (human-gated)**
Proposals that add or remove stages, modify safety rules, or substantially rewrite output schemas are written to `{spec}.pending.yaml` rather than applied directly. A human reviews the diff and applies it with `armature improve <spec> --apply-pending` when ready. This governance boundary ensures auto-improvement never restructures a workflow without deliberate approval.

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

Armature is a production-grade agent harness synthesized from eleven academic papers spanning February 2025–June 2026, plus five governance concepts borrowed from Microsoft's Agent Governance Toolkit. It handles the structural engineering — orchestration, quality control, failure recovery, observability, safety enforcement, and self-improvement — so that every team building on top of it can focus on the domain problem rather than the execution infrastructure.

The sixth paper, AHE ([arXiv:2604.25850](https://arxiv.org/abs/2604.25850)), added accountability to the improvement loop: every spec revision now carries a falsifiable contract, and each subsequent run verifies whether the predicted fixes actually materialized. The harness does not just improve — it explains itself as it does, cycle by cycle.

The seventh paper ([arXiv:2605.26112](https://arxiv.org/abs/2605.26112)) addressed three system-level failure modes the earlier papers left open: stale memory reaching LLMs without warning, context values flowing between stages without provenance, and tool side effects going unverified. It also introduced two architectural additions — a drift score that catches regressions across improvement cycles before they compound, and component governance that separates safe auto-applied changes from structural changes that warrant human review before deployment. The `fan_in: "consensus"` strategy closes the last gap: parallel subagent disagreements are now resolved by a dedicated judge model rather than silently discarded.

The AGT governance layer added five capabilities the academic papers left open: a principled reversibility classification for every tool call, tamper-evident hashing of trace inputs and the governing policy, a human approval gate wired directly into the tool-call path, and a strict fail-closed mode for deployments where the default must be deny rather than allow. These are production governance primitives, not academic proposals — borrowed from an industry team that has been running agentic systems in regulated environments.

The async HTTP service and LangGraph sidecar pattern complete the integration story: Armature is clearly positioned as a **batch-oriented multi-stage work engine**, not a conversational loop. LangGraph owns the conversation; Armature owns the heavy lifting inside each turn. The SSE event stream and latency acknowledgement pattern let chatbot users see progress immediately while multi-stage work runs in the background.

With all eleven papers and the AGT framework implemented, the harness now has:
- **Execution**: DAG orchestration, four role types, parallel fan-out (including consensus synthesis), guided JSON output, model-tier auto-escalation
- **Quality**: HQS metric, consensus deliberation, output schema validation, declarative evaluation stages, post-condition verification
- **Safety**: fail-closed strict mode, five rule actions including human approval, reversibility-based blocking, `ToolBlocked` non-retryable exception
- **Observability**: tamper-evident trace records with inputs hash, policy version, and per-key provenance; OpenTelemetry; run reports with failure signatures; drift score for regression detection
- **Skill execution**: LoRA adapter-backed skills via `skill_library.adapter`, `adapter_support: dynamic`, and the pluggable adapter factory — the Skill-to-LoRA pattern ([arXiv:2606.16769](https://arxiv.org/abs/2606.16769))
- **Continual skill learning**: C-LoRA-style adapter updates via `adapter_factory.continual_learning` to reduce forgetting when new skills or trace batches arrive ([arXiv:2502.17920](https://arxiv.org/abs/2502.17920))
- **Memory**: cross-run persistence, staleness detection, `_stale_memory_keys` warnings, knowledge extraction
- **Self-improvement**: inner refiner loop, outer `SelfImproveRunner`, causal 3-tuple attribution, declared editable surfaces, K-proposal diversity with regression gating, prediction-verification accounting, component governance, SFT/DPO trace export

The eighth paper ([arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1, "Harness Updating Is Not Harness Benefit") addressed a key question about the improvement loop itself: does the evolver need to be as powerful as the executor? The answer is no. The paper shows ≤3.1pp quality difference between frontier and medium-tier evolvers, meaning the `SpecRefiner` can run at medium-tier without quality loss. The paper also introduced two new metrics: Skill-Load Rate (SLR), which measures whether declared tools/skills are actually invoked during execution, and Harness-Following Rate (HFR), which measures whether models adhere to harness instructions on the first attempt. Both are now tracked: `low_skill_activation` is a new `DiagnosticCode` fired when SLR is zero (tools declared but never called), and HFR is the fifth component of the HQS formula (updated weights: `0.35/0.25/0.20/0.10/0.10`).

The ninth paper ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1, "Self-Harness") asked a different question: given that we can diagnose and propose spec changes, how do we make the proposal process *safer and smarter*? Self-Harness introduces four mechanisms that Armature adopted directly. First, causal 3-tuple failure attribution — each diagnostic now carries a `(terminal_cause, causal_status, mechanism)` triple that distinguishes *what* broke from *whose fault it is* from *how it happened*, giving the refiner enough signal to propose structurally targeted fixes rather than generic prompt rewrites. Second, declared editable surfaces — a `self_improvement.editable_surfaces` spec field that bounds what the refiner is allowed to change; surfaces outside the declared set are named in the system prompt as explicitly locked, reducing hallucinated structural modifications. Third, K-proposal diversity — the refiner generates multiple candidates in parallel, each steered by a different diversity hint, and the candidate whose predicted fixes most overlap the active diagnostics is selected; ensemble generation consistently finds better proposals than single-shot refinement. Fourth, held-out trace-split regression gating — stages that have no current diagnostics are treated as a held-out set; proposals that touch those healthy stages are filtered as regression risks before selection, a practical adaptation of the Self-Harness held-in/held-out acceptance criterion to the Armature trace-based setting.

All research and industry-framework gaps closed. ActiveGraph event-architecture concepts adopted (LLM caching, audit replay, trace-triggered behaviors); fork-and-diff and full event sourcing deferred to roadmap.

---

*For implementation details, see `VISION.md`. For integration patterns, see `docs/INTEGRATION.md`. For the full test suite, see `tests/`.*
