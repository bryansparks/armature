# Changelog

All notable changes to Armature are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### ActiveGraph-inspired (arXiv:2605.21997)

- **LLM response caching** — `LLMCache` stores responses by SHA-256 content hash (model + messages + kwargs); `--no-cache` flag on `armature run` bypasses the cache for a clean run. Subsequent runs with identical prompts are instant and free.
- **`armature replay <run_id>`** — reads TraceStore records and renders a stage-by-stage execution table (stage id, role, model, latency, success, quorum score, IHR contribution) with a per-run IHR summary. Enables post-mortem debugging of any historical run without re-executing.
- **`BehaviorRule` / `BehaviorRegistry`** — trace-triggered reactive hooks. Registered rules receive the recent trace list and fire a handler when their pattern matches. Built-in `ihr_feedback` behavior: after runs where rolling IHR drops below 0.75, the engine prints a Rich-formatted hint suggesting `armature improve`.
- **`--auto-improve` flag on `armature run`** — after execution, if IHR < 0.75, automatically calls `SelfImproveRunner.analyze()`. Safe changes are applied in-place to the spec; structural proposals that require review go to `{spec}.pending.yaml`.

### KYA-inspired (arXiv:2605.25376, Veldt Labs)

- **Static spec risk score** — `compute_spec_risk(spec)` scores a HarnessSpec on five weighted factors (tool-call stages, no-judge penalty, require_approval rules, fan-out stages, strict mode credit); returns a `SpecRiskResult` with `score` (0–100), `tier` (LOW / MEDIUM / HIGH / CRITICAL), and `factors` list. Displayed automatically by `armature validate`.
- **Rogue signal counter** — `RogueSignalCounter` dataclass wired into `SafetyHookBuilder.register()`; incremented on every `ToolBlocked` event at runtime. Count appears in the `run_summary` event and in the CLI run output as `"N blocked"`.
- **Only-tighten safety rule validation** — `validate_spec()` now raises `CONFLICTING_SAFETY_RULES` when an `allow` rule targets a tool (or wildcard) that an existing `block` rule already covers. Enforces KYA's composition principle: safety rules may only tighten constraints, never loosen them.

### Tests

- 1,221 tests passing (up from 1,202 at v0.1.0 release)

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

[0.1.0]: https://github.com/elftech/armature/releases/tag/v0.1.0
