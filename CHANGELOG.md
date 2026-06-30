# Changelog

All notable changes to Armature are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added

---

## [0.5.0] - 2026-06-29

### Added

- **`CompiledAgent` bundles may carry `safety_rules`** (`list[ToolSafetyRule]`), merged into the referencing workflow's `safety_rules` at load. Agent `block` rules are a non-overridable floor: workflow `allow` on a blocked tool is dropped and the agent's block is ordered to fire first. Enables `armature-cabinet` 0.2.0 to enforce `brakes.forbidden_actions` from the bundle.
- **`ToolSafetyRule.condition` is now optional** (`None` = applies to every call of the tool). Previously an unconditional rule required a condition that always matched, and the only practical spelling (`{field:"_", op:"truthy"}`) never matched at all.

### Changed

- `_resolve_agent_references` merges bundle `safety_rules` into the spec.
- Bumped version to 0.5.0 (additive minor bump over the released v0.4.0) and reconciled the stale `armature/__init__.py` (`__version__` was `0.3.5`) with `pyproject.toml`.

---

## [0.4.0] — 2026-06-29

### Added

- **Pluggable adapter factory + LoRA adapter skills** — `skill_library` entries can reference a registered LoRA adapter via `skill.adapter`. On tiers with `adapter_support: dynamic`, the adapter artifact is passed to the provider at runtime and the skill text is omitted from the prompt to save context; on `none` tiers the configured `fallback` policy applies. Includes `mock`, `s2l`, `trace`, `local`, `remote`, and `merged` backends; `AdapterRegistry` for versioned local storage; `MergedAdapterFactory` for parameter-space adapter merging; CLI commands `armature adapter create/promote/merge/eval`; and `watch --tune` skeleton. The pattern is developed from the **Skill-to-LoRA** paper (Zhang & Qi, CUHK, June 2026 — [arXiv:2606.16769](https://arxiv.org/abs/2606.16769)).
- Example `examples/07_lora_adapter.yml` demonstrating a mock-backed TDD skill adapter end-to-end.
- `--registry` option on `armature run` and `Harness.from_spec(...)` so workflows can use a custom adapter registry without relying on the default `~/.armature/adapters`.
- **`use_dora` + `continual_learning` adapter training options** — `adapter_factory.use_dora` enables DoRA; `adapter_factory.continual_learning` enables C-LoRA-style sequential adapter updates with frozen `R_old`, near-zero `R_delta`, and orthogonality regularizer (Zhang et al., [arXiv:2502.17920](https://arxiv.org/abs/2502.17920)). Flags propagate through all adapter backends and are persisted in `adapter_config.json` / `AdapterMetadata`.
- **Real mathematical LoRA adapter merging** — `MergedAdapterFactory` now loads source `adapter.safetensors` files and adds corresponding `lora_A`, `lora_B`, and (for DoRA) `lora_magnitude_vector` tensors. Supports optional per-source weights and enforces strict compatibility checks (same base model, rank, alpha, target modules, DoRA setting).
- **Continual adapter learning lifecycle** — all training backends resolve a prior adapter version automatically when `continual_learning` is enabled, validate compatibility, and pass the prior artifact directory to the trainer as a warm start. A production trainer can load the prior LoRA and add a near-zero delta with an orthogonality regularizer (C-LoRA).
- **Trace preprocessing for adapter training** — `PreprocessConfig` and `preprocess_examples` support score filtering, maximum conversation length, exact-message deduplication, and example caps. Configure via `request.extra["preprocess"]` or the `armature adapter create`/`update` pipeline.
- **Promotion policies** — `PromotionPolicy` abstraction with `AlwaysPromotePolicy`, `ThresholdPromotionPolicy`, and `CompositePromotionPolicy`. `AdapterRegistry.register()` and `AdapterRegistry.promote()` consult a policy before advancing the `latest` pointer. CLI gains `--min-score` and `--force` flags on `armature adapter promote`.
- **`armature adapter update` command** — single-shot command that trains a new adapter version from traces, optionally evaluates it against a workflow spec, and promotes it only if the configured policy passes. Derives hyperparameters from the prior `latest` version so continual updates are one command.
- **Agent + skill attribution on `TraceRecord`** — each LLM-stage trace now carries `agent_id`, `agent_version`, and `active_skill_ids`, persisted as new SQLite columns with an additive, per-column migration guard (existing databases upgrade in place). The engine populates them from the resolved `agent_library` bundle role (`x_source` → `agent_id`, `x_agent_version` → `agent_version`, `skills` → `active_skill_ids`); they are absent for inline `role:` stages. Per-skill tool attribution is derivable by joining `active_skill_ids` × the skill library's per-skill tools × the recorded `tools_called`.
- **`armature loop <spec>` outer-loop driver** — new CLI subcommand that runs a workflow back-to-back under a central budget (`--max-iterations`, `--max-llm-calls`, `--max-wallclock`, `--max-tokens`), with carry-forward between passes (`--carry-forward`, `--inject-as`), a `--until` Jinja2 stop predicate, `--converge` early stop on identical consecutive results, and `--interval` pacing. Each iteration is a fresh `Harness.run()` with its own run_id; the driver accounts the budget from the TraceStore and writes one `__loop__` summary trace row (run_id = the loop session id, so per-iteration HQS is not inflated). `--output` writes the full `LoopResult` as JSON. New `armature/loop/` package (`carry.py`, `logic.py`, `runner.py`) + an additive `loop` command in `cli.py`; **no changes to the engine, spec models, or validator**. Closes the per-iteration trace/report fragmentation, missing central budget, and runaway-loop risks called out in `ROADMAP.md` without the engine changes `IterateConfig` would require.

### Docs

- README, `docs/USER-GUIDE.md`, `docs/ARMATURE-SPEC-REF.md`, `docs/ARCHITECTURE.md`, and `docs/ARMATURE-PHILOSOPHY.md` now cite the Skill-to-LoRA and C-LoRA papers as the research foundation for adapter-backed skills and continual adapter learning.
- New guide `docs/ADAPTER-POWERED-TEAMS.md` explains end-to-end how to set up candidate SLM tiers, declare adapter-backed skills, create adapters, and run periodic/continual retraining from traces.
- `armature loop` documented in README §CLI, `docs/USER-GUIDE.md` §18 (Running workflows → CLI reference), and `CLAUDE.md` CLI Quick Reference. A new *Agent attribution fields* subsection in `docs/USER-GUIDE.md` documents `agent_id` / `agent_version` / `active_skill_ids`. `ROADMAP.md` notes `armature loop` as a no-engine-change option that addresses the outer-loop budget and merged-report gaps.

---

## [0.3.5] — 2026-06-17

### Added

- **`agent_library` + `Stage.agent`** — specs can now reference pre-built agent bundles instead of inlining a role. Declare bundles in `agent_library` (each entry points to an `agent.yaml` file with a `role` and optional `skill_library`); stages reference them via `agent: <key>`. At load time, `loader._resolve_agent_references` copies the bundle's role onto the stage, merges bundle skills into `spec.skill_library` (existing keys win), normalises skill paths to absolute, and clears `stage.agent`. Zero engine changes — the resolved spec is indistinguishable from a hand-authored one. This is the first building block for Armature Cabinet, a future registry of shareable, versioned agent definitions.

---

## [0.3.4] — 2026-06-15

### Fixed

- **`armature improve` / `armature optimize` crashed for non-Anthropic users** — both commands hardcoded `claude-sonnet-4-6` as the refiner model, raising an auth error for anyone without `ANTHROPIC_API_KEY`. `SelfImproveRunner` now resolves the refiner model lazily from the spec's own top tier (`frontier` → `large` → `medium` → `small` → `tiny` → first custom tier), so improvement runs on whatever provider the workflow already uses. The `ARMATURE_REFINER_MODEL` env var overrides this for users who want a dedicated refiner model. `OptimizerRunner` receives the same treatment via an `ARMATURE_REFINER_MODEL` env var that patches the optimizer's internal spec at runtime. Both commands also gain `--model` CLI flags for explicit override.
- **Missing friendly-error handling on `improve` / `optimize` / `--auto-improve`** — auth and connectivity failures in these commands emitted a raw traceback instead of the friendly `✗ No valid API key` message that `armature run` already showed. All three paths now route through `_print_provider_error`.

---

## [0.3.3] — 2026-06-15

### Fixed

- **Checkpoint + loop resume bug** — when a `loop` stage was checkpointed, `_execute_stage_with_recovery` would return the cached result from iteration 1 on every subsequent iteration, short-circuiting the loop. Fixed by writing per-iteration checkpoint keys (`stage_id__iter_N`) and loading them into a separate `_checkpoint_loop_iters` dict; completed loops also write the final result under the plain `stage.id` key so downstream stages can reference it. Added 6 new tests in `tests/runtime/test_checkpoint.py` covering full execution, mid-loop resume, and full-loop skip.

---

## [0.3.2] — 2026-06-15

### Changed

- **Renamed the run-quality metric from IHR (Implicit Harness Rating) to HQS (Harness Quality Score)** throughout the codebase, CLI/dashboard output, and documentation. The formula and components are unchanged — only the name. This removes an acronym collision with the unrelated "Intelligent Harness Runtime (IHR)" from the NLAH paper, since Armature's metric is its own composite quality score and was never derived from that work. Public symbols renamed: `IhrResult` → `HqsResult`, `compute_ihr` → `compute_hqs`, the `ihr` field → `hqs`; the `--auto-improve` threshold is now described as "HQS < 0.75".

---

## [0.3.1] — 2026-06-15

### Changed

- `armature run` now translates common LLM-provider failures (missing/invalid API key, unreachable provider, rate limit) into a concise, actionable one-line message instead of dumping a raw multi-hundred-line litellm traceback. Unexpected errors still surface a full traceback.

### Docs

- README: documented the `[wizard]` extra (required by `armature new`); corrected the `armature improve` flags (`--apply`/`--no-apply`); made the no-API-key Ollama quickstart self-contained so it works for pip-only installs; added a provider-key note to the Quick start.

---

## [0.3.0] — 2026-06-13

### Added

- `loop` configuration on `Stage` for deliberate iteration, distinct from `on_fail.loop` retry
- `IterationConfig` model: `max_iterations`, `until`, `carry_forward`, `iteration_var`, `backoff_s`, `backoff_max_s`
- `_iteration` context variable: always defined (1-based), includes `num`, `is_first`, `is_last`, `carry_forward`
- `carry_forward` for selective state passing between iterations; `null` carries entire previous result
- `loop_iteration` trace event type and `TraceRecord.loop_iteration` field (distinct from `retry_attempt`)
- Validator checks for `IterationConfig`: `max_iterations >= 1`, valid Jinja2 `until`, non-empty `carry_forward` paths, valid `iteration_var` identifier, warning when both `loop` and `on_fail.loop` are set
- Risk scoring: +4 per `loop` stage, +2 per `loop` stage with `max_iterations > 10`
- Example workflow `examples/11_iterative_refinement.yml` demonstrating the `loop` feature

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
- **`armature replay <run_id>`** — reads TraceStore records and renders a stage-by-stage execution table (stage id, role, model, latency, success, quorum score, HQS contribution) with a per-run HQS summary. Enables post-mortem debugging of any historical run without re-executing.
- **`BehaviorRule` / `BehaviorRegistry`** — trace-triggered reactive hooks. Registered rules receive the recent trace list and fire a handler when their pattern matches. Built-in `hqs_feedback` behavior: after runs where rolling HQS drops below 0.75, the engine prints a Rich-formatted hint suggesting `armature improve`.
- **`--auto-improve` flag on `armature run`** — after execution, if HQS < 0.75, automatically calls `SelfImproveRunner.analyze()`. Safe changes are applied in-place to the spec; structural proposals that require review go to `{spec}.pending.yaml`.

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
- **Harness-Following Rate (HFR)** added as a fifth HQS component (10% weight). HFR = fraction of trajectories where the model adheres to harness instructions on the first attempt (`escalation_count == 0`). HQS formula updated from `0.40/0.30/0.20/0.10` to `0.35/0.25/0.20/0.10/0.10` (output_valid / success / quorum / latency / hfr). Both `TraceStore.compute_hqs` and `SelfImproveRunner._compute_hqs` updated.
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
- HQS (Harness Quality Score) — 4-component quality metric
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

[0.3.3]: https://github.com/bryansparks/armature/releases/tag/v0.3.3
[0.3.0]: https://github.com/bryansparks/armature/releases/tag/v0.3.0
[0.2.0]: https://github.com/bryansparks/armature/releases/tag/v0.2.0
[0.1.0]: https://github.com/bryansparks/armature/releases/tag/v0.1.0
