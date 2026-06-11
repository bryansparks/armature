# Changelog

All notable changes to Armature are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Self-Harness-inspired ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1)

- **Causal 3-tuple failure attribution** — `DiagnosticResult` now carries a `causal_attribution: CausalAttribution` field with three orthogonal dimensions: `terminal_cause` (execution_error / schema_validation / low_confidence / postcondition / prompt_weak), `causal_status` (spec_problem / model_problem / tool_problem), and `mechanism` (timeout / runtime_error / schema_too_strict / model_underpowered / judge_uncertain / tier_insufficient / tool_violation / prompt_missing_instruction). The refiner can target fixes at the mechanism rather than guessing from the surface symptom.
- **Declared editable surfaces** — new `self_improvement:` top-level spec field with `editable_surfaces` list. The five surfaces are `descriptions`, `schemas`, `model_tiers`, `retry_counts`, `timeouts`; defaults are `[descriptions, retry_counts, timeouts]`. `SpecRefiner` system prompt is dynamically generated to restrict the model to the declared surfaces and explicitly name locked ones.
- **K-proposal diversity** — `SelfImproveRunner` accepts `n_proposals: int` (default 1). When `n_proposals > 1`, `SpecRefiner.refine_many()` generates candidates in parallel via `asyncio.gather` with rotating diversity hints (minimize changes / fix output format / adjust model tier / tighten schema). `_pick_best_proposal()` selects the candidate whose `predicted_fixes` most overlap the active diagnostics.
- **Held-out trace-split regression gating** — `_healthy_stage_ids()` identifies stages that appear in traces but carry no diagnostics. `_proposal_regression_risk()` flags proposals that modify those healthy stages. Risky proposals are filtered before selection; if all candidates are risky the best of the risky set is used as a fallback. `ImprovementReport` gains `n_proposals_generated` and `regression_risk_count` fields (also written to the JSONL audit log).

### Tests

- 1,388 tests passing (up from 1,286 at v0.2.0)

---

## [0.2.0] — 2026-06-07

### ActiveGraph-inspired ([arXiv:2605.21997](https://arxiv.org/abs/2605.21997))

- **LLM response caching** — `LLMCache` stores responses by SHA-256 content hash (model + messages + kwargs); `--no-cache` flag on `armature run` bypasses the cache for a clean run. Subsequent runs with identical prompts are instant and free.
- **`armature replay <run_id>`** — reads TraceStore records and renders a stage-by-stage execution table (stage id, role, model, latency, success, quorum score, IHR contribution) with a per-run IHR summary. Enables post-mortem debugging of any historical run without re-executing.
- **`BehaviorRule` / `BehaviorRegistry`** — trace-triggered reactive hooks. Registered rules receive the recent trace list and fire a handler when their pattern matches. Built-in `ihr_feedback` behavior: after runs where rolling IHR drops below 0.75, the engine prints a Rich-formatted hint suggesting `armature improve`.
- **`--auto-improve` flag on `armature run`** — after execution, if IHR < 0.75, automatically calls `SelfImproveRunner.analyze()`. Safe changes are applied in-place to the spec; structural proposals that require review go to `{spec}.pending.yaml`.

### KYA-inspired ([arXiv:2605.25376](https://arxiv.org/abs/2605.25376), Veldt Labs)

- **Static spec risk score** — `compute_spec_risk(spec)` scores a HarnessSpec on five weighted factors (tool-call stages, no-judge penalty, require_approval rules, fan-out stages, strict mode credit); returns a `SpecRiskResult` with `score` (0–100), `tier` (LOW / MEDIUM / HIGH / CRITICAL), and `factors` list. Displayed automatically by `armature validate`.
- **Rogue signal counter** — `RogueSignalCounter` dataclass wired into `SafetyHookBuilder.register()`; incremented on every `ToolBlocked` event at runtime. Count appears in the `run_summary` event and in the CLI run output as `"N blocked"`.
- **Only-tighten safety rule validation** — `validate_spec()` now raises `CONFLICTING_SAFETY_RULES` when an `allow` rule targets a tool (or wildcard) that an existing `block` rule already covers. Enforces KYA's composition principle: safety rules may only tighten constraints, never loosen them.

### Long-horizon focus

- **`mission:` field on HarnessSpec** — a single-line or multi-paragraph statement of the workflow's overall goal. Automatically injected into every LLM stage's system prompt as a `[Workflow Mission]` block, followed by a `[Prior stages]` breadcrumb (compact JSON preview of each completed stage's output). Keeps agents anchored to the stated goal across long-running workflows (hours or days) without any per-stage configuration. Non-LLM stages are unaffected.

### Low-latency / streaming

- **`response_stage: true` on a Stage** — designates a single text-mode LLM stage as the streaming response. When the HTTP service executes the workflow, tokens from that stage are forwarded to the SSE event stream in real time as `{"type": "token", "content": "..."}` events. A `{"type": "response_stage_complete", "stage_id": "...", "content": "<full text>"}` event fires as soon as the response is assembled, before background stages finish. Clients can render the response immediately without waiting for `run_complete`. JSON-mode stages (`output_mode: json` / `guided_json`) silently ignore `response_stage: true` and use the normal non-streaming path.

### Named workflow registry

- **`WorkflowRegistry`** (`armature/service/registry.py`) — in-memory registry of named `HarnessSpec` objects. `load_dir(path)` scans a directory for `*.yaml`/`*.yml` specs and keys each by `spec.name`, silently skipping malformed files. `register(spec)` adds a single spec; `get(name)` returns a spec or `None`; `list_all()` returns a list of `{name, description, stages}` dicts for API enumeration.
- **`build_app(registry)` factory** — `armature/service/app.py` is refactored so the FastAPI application is constructed from an injected `WorkflowRegistry`. The module-level `app = build_app()` default is preserved for backward compatibility with existing deployments.
- **`GET /workflows`** — list all registered workflows (name, description, stage count).
- **`GET /workflows/{name}`** — workflow metadata (name, description, version, stages list).
- **`POST /workflows/{name}/run`** — synchronous run; accepts `{"inputs": {...}}`; returns `{run_id, status, result}`.
- **`POST /workflows/{name}/run/async`** — async run; returns `{job_id, status}`; poll or stream results via the existing `GET /run/{job_id}` and `GET /run/{job_id}/events` endpoints.
- **`armature serve --specs-dir <path>`** — new flag that loads all YAML specs from the given directory into a `WorkflowRegistry` at startup and prints the registration count. Existing `armature serve` (no flag) is unaffected.

### Long-horizon state & triggers

- **`continuation:` spec block** — enables long-horizon workflows that remember outputs across activations. `carry_forward` lists stage keys from a prior run to pull forward; `inject_as` names the context key they appear under (default: `prior_run`). `Harness._load_prior_context()` resolves the last completed run via `TraceStore.get_run_outputs()` and merges the values into the initial context before the DAG executes.
- **Output truncation cap raised (200 → 2000 chars)** for carry-forward stages — ensures meaningful summaries survive the round-trip without being silently clipped.
- **`triggers:` spec block** — `CronTrigger` and `WebhookTrigger` models added to `HarnessSpec` with full Pydantic validation. Model and validation layer only; firing is handled by `armature watch`.
- **`armature watch <spec>`** — daemon command that blocks until Ctrl-C. `TriggerDispatcher` runs a `_cron_loop` (powered by `croniter`) for scheduled triggers and a Starlette-backed `_webhook_server` for HTTP triggers; both fire `Harness.run()` on each event. `croniter>=2.0` added as a core dependency.

### Research-backed improvements ([arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1)

- **Cheap-evolver for `SelfImproveRunner`** — spec refinement now explicitly uses a medium-tier model (not the frontier). [arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1 shows ≤3.1pp quality difference between frontier and medium-tier evolvers; the cost savings are substantial. `SpecRefiner`'s docstring updated to reflect this intentional design choice.
- **Harness-Following Rate (HFR)** added as a fifth IHR component (10% weight). HFR = fraction of trajectories where the model adheres to harness instructions on the first attempt (`escalation_count == 0`). IHR formula updated from `0.40/0.30/0.20/0.10` to `0.35/0.25/0.20/0.10/0.10` (output_valid / success / quorum / latency / hfr). Both `TraceStore.compute_ihr` and `SelfImproveRunner._compute_ihr` updated.
- **Skill-Load Rate (SLR) diagnostic** — new `low_skill_activation` `DiagnosticCode` fires when a stage declares tools in `role.tools` but the model never invokes any. `TraceRecord` gains `tools_declared` and `tools_called` fields; `LLMNode` collects called tool names during the ReAct loop and passes them to the engine for trace recording. `SpecRefiner` system prompt updated to advise strengthening role descriptions when `low_skill_activation` is detected.

### Tests

- 1,286 tests passing (up from 1,202 at v0.1.0 release)

---

## [0.1.0] — 2026-05-26

Initial public release.

### Core execution

- YAML workflow spec format with full Pydantic validation
- Async DAG executor (Kahn's algorithm, `asyncio.gather` for parallel stages)
- Four role types: `worker`, `orchestrator`, `judge`, `researcher`
- Model tier routing with automatic escalation on validation failure
- `on_fail` retry loops with context enrichment (`_retry_attempt`, `_last_error`)
- Guided JSON output via litellm `response_format`
- Human approval gate (`gate: human`)
- Script and Python adapter nodes for deterministic steps
- LLM retry with exponential backoff (rate limits, service unavailability)
- Context management and compaction

### Parallel execution

- `SubagentNode` fan-out/fan-in with `asyncio.gather`
- Fan-in strategies: `list`, `merge`, `first`, `consensus`
- `fan_in: "consensus"` — LLM judge synthesizes conflicting parallel outputs
- `partition_key` to split a context list across N child agents
- `Stage.isolated` — strip parent context to declared inputs only

### State and observability

- `TraceStore` (SQLite) — structured per-stage trace records
- `SessionLog` (JSONL) — append-only event log, crash-safe
- `ArtifactStore` — file-backed output persistence
- IHR (Implicit Harness Rating) — 4-component quality metric
- `TraceRecord.inputs_hash` — SHA-256 fingerprint of stage inputs (tamper-evident)
- `TraceRecord.policy_version` — SHA-256 of active safety rules
- `TraceRecord.inputs_provenance` — per-key origin labels for every context value
- OpenTelemetry instrumentation (optional, zero overhead if SDK absent)
- `armature report --run-id <id>` — per-run text report with failure signatures
- `armature dashboard <spec>` — Rich 4-panel aggregate health dashboard

### Safety and governance

- `ToolSafetyRule` + `SafetyCondition` declarative YAML DSL
- Six operators: `contains`, `not_contains`, `equals`, `not_equals`, `matches_regex`, `truthy`
- Five actions: `block`, `warn`, `log`, `require_approval`, `allow`
- `ToolBlocked` non-retryable exception
- `safety_mode: strict` — fail-closed (deny on no-match)
- `Reversibility` enum (`FULL / PARTIAL / NONE`) on every tool
- `_tool_reversibility` pseudo-field queryable in safety rule conditions
- `ToolDescriptor.postcondition` — callable to verify tool side effects
- `PostconditionFailed` exception and `POSTCONDITION_FAILED` diagnostic code

### Memory

- `MemoryStore` — cross-run persistent memory (SQLite)
- `MemoryConfig.fresh` — opt-out of prior memories for a clean-slate run
- `MemoryStore.staleness_threshold_days` — flag aged memory entries
- `_stale_memory_keys` context injection when stale entries are detected
- `KnowledgeStore` — post-stage structured knowledge extraction

### Self-improvement

- `DiagnosticAnalyzer` — 4-code failure signature taxonomy
- `Stage.post_run` — in-run refiner stage receives `_transcript` + `_diagnostics`
- `SelfImproveRunner` — outer cross-run improvement loop (`armature improve`)
- `SpecRefiner` — frontier LLM rewrites targeted YAML sections
- `ImprovementReport` — improvement audit with drift score and governance classification
- `_load_all_verified_fixes()` — cross-cycle regression detection (drift score)
- `_classify_changes()` — auto-apply vs. review-required classification; writes `.pending.yaml`
- `RefinerResult` — carries `predicted_fixes` and `predicted_regressions`
- `_verify_predictions()` — set-math verification of prior predictions
- `ProposalStore` + `OptimizerRunner` — multi-iteration meta-harness optimizer
- `PromptBootstrapper` — injects high-quality trace examples as few-shot prompts
- `SpecDrafter` + `AutoHarness` — NL-to-spec synthesis loop
- `TraceExporter` — SFT and DPO training data export (`armature export-traces`)

### Ecosystem

- LangFuse adapter (auto-activates from env vars)
- LangSmith adapter (auto-activates from env vars)
- MCP server support (`mcp_servers:` spec section, stdio/http/sse transports)
- FastAPI HTTP service — sync (`POST /run`) and async (`POST /run/async`, SSE stream)
- LangGraph sidecar template (Docker Compose)
- Messaging channel connectors (`armature channels start`)
- Docker sandbox isolation (`sandbox: mode: docker`)
- Hermes emitter for agent bundle generation

### Developer experience

- `armature validate <spec>` — validate without running
- `armature new` — interactive spec creation wizard
- `armature doctor` — environment health check
- Skills system (`skill_library:` in spec, injected per-stage)
- `armature serve` — HTTP service
- `armature optimize <spec>` — single-shot optimizer
- `armature improve <spec> [--apply-pending]` — self-improvement loop
- `armature export-traces` — trace export for fine-tuning
- `armature dashboard <spec> [--watch] [--format json]` — health dashboard
- 1,202 tests

[0.2.0]: https://github.com/bryansparks/armature/releases/tag/v0.2.0
[0.1.0]: https://github.com/bryansparks/armature/releases/tag/v0.1.0
