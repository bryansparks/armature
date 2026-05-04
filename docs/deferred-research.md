# Armature — Deferred Research Items

Items from the foundational papers that were intentionally scoped out of Phases 1 and 2.
Each section identifies the source, what was skipped, and the rough effort/value tradeoff.

---

## Source Papers

| Paper | Citation | Status |
|-------|----------|--------|
| **NLAH** — Natural Language Agent Harnesses | Tsinghua, arXiv:2603.25723 | ~80% implemented |
| **Meta-Harness** — Trace-Based Outer-Loop Optimization | Stanford / Khattab et al., arXiv:2603.28052 | ~30% implemented |
| **Harness Survey** — 6-Component Completeness Model | arXiv:2604.0428 | ~95% implemented |

---

## From NLAH (arXiv:2603.25723)

### 1. IHR — Implicit Harness Rating

**What it is:** A formal metric for measuring harness quality from execution data, distinct from task-level accuracy. The paper proposes IHR as a structured signal: how often does the harness self-correct, how cleanly do stages complete, how often does it loop unnecessarily?

**What we have instead:** `quorum_score` in TraceStore, `output_valid`, `latency_ms`. These are the raw signals but there's no aggregated IHR computation.

**Why it matters:** Without IHR, the optimizer's `analyze_traces` stage is doing unstructured pattern matching on raw trace data. A formal IHR score per run would give the optimizer a single objective to drive toward.

**Effort:** Medium. Define the IHR formula from the paper, compute it in `TraceStore.compute_ihr(run_id)`, surface it in the optimizer workflow context. Likely ~1 day.

---

### 2. Runtime `on_fail` Loop Recovery

**What it is:** When a stage fails its quality check, the spec can declare a recovery loop: `on_fail: loop: { stage: deliberate, context: enrich, max: 3 }`. The harness re-executes the specified stage with enriched context, up to `max` times.

**What we have instead:** `on_fail` is parsed into the Stage model (it's in the schema), but `engine.py` raises an exception rather than executing the recovery loop.

**Why it matters:** This is NLAH's primary mechanism for self-correcting workflows. Without it, any stage failure terminates the run. With it, quality degradation triggers targeted retry with richer context.

**Effort:** Medium-high. The engine needs a retry loop around `_execute_stage`, passing the `on_fail.loop` spec to re-queue the named stage with merged context. Needs careful interaction with the DAG executor. ~2–3 days.

---

### 3. NL-to-Spec Generation

**What it is:** Given a natural language task description, generate a valid NLAH/Armature YAML spec. The paper describes this as an outer-loop step where a frontier model translates user intent into a structured harness spec.

**What we have instead:** The Phase 3 Armature Editor is planned to include this, but it's not designed or implemented.

**Why it matters:** This closes the loop from the paper's core claim — that NL specs outperform code specs. If generating specs from NL is cheap, the whole workflow-authoring experience becomes conversational rather than YAML-authoring.

**Effort:** High (design) + Medium (implementation). The generation is a frontier LLM call; the hard part is prompt engineering and validation against the Pydantic schema. The Editor is the natural home for this. ~1 week for a good prompt + validation pipeline.

---

### 4. Parallel Fan-Out with Fan-In Aggregation

**What it is:** Running multiple subagents in parallel (not sequential) and merging their results. The NLAH paper describes true parallel fan-out where N subagents execute simultaneously and a coordinator aggregates their outputs.

**What we have instead:** `SubagentNode` dispatches a single child workflow. The DAG executor supports parallel stage execution (Kahn's algorithm, `asyncio.gather`), but a single stage can only launch one child.

**Why it matters:** Embarrassingly parallel tasks (N-way document processing, ensemble voting, multi-perspective analysis) require true fan-out. This is the basis for scaling worker throughput.

**Effort:** Medium. Add a `subagent_count: N` field to Stage; `SubagentNode` launches N children via `asyncio.gather` with partitioned context; results merged into a list. ~1–2 days for core, plus edge cases in result merging.

---

## From Meta-Harness (arXiv:2603.28052)

### 5. Automatic Prompt Bootstrapping

**What it is:** DSPy's core mechanism. Given a metric function and a few labeled examples, the optimizer automatically generates few-shot demonstrations for each stage's prompt. It runs the pipeline forward, filters high-scoring examples, and prepends them to the system prompt.

**What we have instead:** The optimizer proposes YAML diffs (e.g., "add output_mode: guided_json") but doesn't generate or inject few-shot examples into prompts.

**Why it matters:** Bootstrapped few-shot prompts typically improve SLM performance 10–30% on structured tasks without any fine-tuning. It's the highest-leverage prompt improvement that doesn't require Alembic.

**Effort:** High. Requires: (1) storing stage I/O examples alongside traces, (2) a bootstrap optimizer that filters high-quality examples, (3) a prompt assembler extension that injects examples into the system prompt for a given stage. ~1 week.

**Connection to existing code:** `TraceStore.high_quality_traces()` already identifies the right candidates. The missing piece is extraction + injection.

---

### 6. Compile-Time Optimization (Teleprompters)

**What it is:** The Meta-Harness treats the harness spec as a program and *compiles* it — running multiple optimization passes offline before deployment. Teleprompters (now called "optimizers" in DSPy) search the prompt space systematically using the metric function.

**What we have instead:** The optimizer runs *post-hoc* on production traces and proposes a single diff per run. It's reactive, not proactive. There's no offline compile step.

**Why it matters:** Compile-time optimization is more thorough than trace-reactive optimization because it can explore prompt variations before any user sees the workflow. It's the difference between pre-production tuning and production incident response.

**Effort:** Very high. This requires a full optimization loop: dataset of labeled examples + metric function + systematic perturbation of prompts/specs + evaluation harness to score variants. This is DSPy's entire value proposition rebuilt for YAML specs. Likely 2–4 weeks for a meaningful implementation.

**Note:** This may be better served by using DSPy directly for individual stages and calling DSPy-compiled modules from Armature nodes, rather than reimplementing DSPy inside Armature.

---

### 7. Metric-Driven Spec Optimization

**What it is:** The Meta-Harness optimizer takes a user-defined metric function (e.g., `lambda result: result["confidence"] > 0.9 and result["latency_ms"] < 500`) and searches spec space to maximize it. The metric drives which spec variants are accepted.

**What we have instead:** The `evaluate_proposal` stage is a frontier judge making a subjective call on the proposed diff. There's no formal metric; acceptance is LLM judgment.

**Why it matters:** LLM-as-judge is noisy. A programmatic metric is deterministic and auditable. For workflow types where success criteria can be expressed as code (response time + quality score thresholds), a metric function would make the optimizer dramatically more reliable.

**Effort:** Medium. Add a `metric_fn` parameter to `OptimizerRunner`; if provided, evaluate proposed spec variants against the metric rather than (or in addition to) the judge stage. The hard part is safely executing user-provided metric functions. ~2–3 days.

---

### 8. Spec Version A/B Testing

**What it is:** The optimizer proposes a diff; instead of a judge accepting/rejecting it, the harness runs both the original and proposed spec variants against the same inputs and compares empirical outcomes.

**What we have instead:** The `evaluate_proposal` stage makes a judgment call. There's no empirical comparison.

**Why it matters:** A/B testing is the only statistically sound way to validate spec changes. Judge acceptance is necessary for initial filtering but not sufficient for production deployment decisions.

**Effort:** Medium. `OptimizerRunner.a_b_test(original_spec, proposed_spec, inputs_sample)` runs both specs N times on held-out inputs and returns a comparison report. The infrastructure (Harness, TraceStore) already supports this; it's orchestration logic. ~1–2 days.

---

### 9. Cross-Stage Typed Signature Chaining

**What it is:** DSPy enforces that if stage A outputs `{ brief: ResearchBrief }` and stage B inputs `{ brief: ResearchBrief }`, the type `ResearchBrief` is the same Pydantic model in both directions — caught at spec load time, not at runtime.

**What we have instead:** `Signature.input` and `Signature.output` are `dict[str, str]` — free-form key/type name pairs. Type names are strings, not references to actual types. There's no cross-stage type compatibility check.

**Why it matters:** Mismatched stage signatures are a common authoring bug that currently fails at runtime (when the LLM output doesn't match the next stage's expected input shape) rather than at spec load time.

**Effort:** Medium. Spec loader needs a validation pass: for each `depends_on` edge, check that the upstream stage's output signature keys are available and type-compatible with the downstream stage's input signature. Requires formalizing the type name registry. ~2–3 days.

---

## LangFuse / Observability (Deferred Decision)

**Context:** LangFuse provides a human-facing observability dashboard for LLM applications (traces, spans, generations, scores). Decision was to defer until Phase 3 OpenTelemetry work is planned, then use LangFuse as an OTel backend rather than a direct SDK integration.

**Planned approach when implemented:**
1. Phase 3: Implement OTel instrumentation in the engine (each `run()` → OTel trace, each stage → span, each LLM call → generation span with token counts).
2. Point OTel exporter at LangFuse's OTel endpoint (`OTEL_EXPORTER_OTLP_ENDPOINT`).
3. LangFuse becomes one of many possible backends (Jaeger, Grafana Tempo, etc.).

**Why not direct SDK:** Avoids LangFuse lock-in. OTel is the standard; LangFuse is one consumer of it.

---

## Priority Ordering (Suggested)

When returning to this list, suggested order by impact/effort ratio:

| # | Item | Impact | Effort | Phase |
|---|------|--------|--------|-------|
| 1 | `on_fail` runtime recovery loop | High | Medium | 3 |
| 2 | IHR computation | Medium | Low | 3 |
| 3 | A/B spec testing | High | Medium | 3 |
| 4 | Metric-driven optimization | High | Medium | 3 |
| 5 | Prompt bootstrapping from traces | High | High | 4 |
| 6 | Parallel fan-out / fan-in | Medium | Medium | 4 |
| 7 | Cross-stage signature chaining | Medium | Medium | 4 |
| 8 | NL-to-spec generation | High | High | Editor |
| 9 | Compile-time optimization (teleprompters) | Very High | Very High | 4+ |
| 10 | LangFuse via OTel | Medium | Low | 3 |

*Phase 3 = Spec Editor + Production Hardening. Phase 4 = assumed post-Editor optimization work.*

---

*Last updated: 2026-05-04. Reference: VISION.md ADR-006, research papers cited in Research Foundations section.*
