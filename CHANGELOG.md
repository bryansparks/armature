# Changelog

All notable changes to Armature are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

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
