# HQS and Self-Improvement in Armature

Quality measurement and trace-driven spec evolution — no manual tuning required.

---

Most agentic systems fail silently. A stage starts returning invalid JSON, a judge loses confidence, escalations pile up — and you find out weeks later when a human notices something looks off. There's no error rate, no p99 latency, no signal to watch. You're flying blind.

Armature's answer is HQS: the **Harness Quality Score**. It's a single composite score, computed over accumulated trace data, that reflects how well your workflow is actually performing. And when HQS falls below a target threshold, `armature improve` closes the loop automatically — loading the traces, diagnosing what broke, calling a language model to propose targeted YAML revisions, and applying the safe changes without human intervention.

Every run makes the next run better. That's the design.

---

## HQS — the metric

HQS is a weighted composite of five observable signals, all derived from traces recorded during normal workflow execution:

```
HQS = 0.35 × output_valid_rate
    + 0.25 × success_rate
    + 0.20 × avg_quorum_score
    + 0.10 × latency_score
    + 0.10 × happy_path_rate
```

The weights reflect what matters most. Structural output validity (0.35) dominates because downstream stages cannot compose on malformed data — a stage that times out cleanly is less damaging than one that silently returns the wrong shape. Success rate (0.25) is the next concern: outright failures destroy the run. Quorum consensus (0.20) captures confidence across fan-out stages. Latency and escalation-free runs each contribute 10%.

### The five components

**`output_valid_rate`** — fraction of stages whose output matched the declared `output_schema`. If your spec declares `output_schema: {required: [risk_level, issues]}` and the LLM returns `{"risk": "low"}`, that trace is invalid. High weight because downstream Jinja2 expressions like `{{ analyst.risk_level }}` silently produce empty strings when the key is missing — a failure that doesn't look like one.

**`success_rate`** — fraction of stages that completed without error or timeout. Captures hard failures: exceptions, API errors, `on_fail` exhaustion.

**`avg_quorum_score`** — average consensus level across fan-out stages that use `fan_in: consensus`. 1.0 means all workers agreed; 0.5 is noise-floor (random disagreement). Stages without a quorum score (non-fan-out stages) contribute 0.5 to this average by default. A persistently low quorum score on a judge stage is a signal that the prompt is ambiguous or the task is genuinely hard.

**`latency_score`** — `max(0, 1 - avg_latency_ms / 5000)`. Stages completing in under 5 seconds score well; stages taking 10+ seconds score near zero. Not a pure performance metric — a slow but correct stage is better than a fast but wrong one, which is why latency only contributes 10%.

**`happy_path_rate`** — fraction of individual stage executions that required zero escalations. A stage that always hits `on_fail.loop` before succeeding still counts as a success but degrades this score. It's a proxy for workflow confidence: a well-written spec shouldn't need to retry constantly.

### What a good HQS looks like

| HQS | Interpretation |
|-----|----------------|
| 0.95+ | Production-grade. Rare failures, clean output, fast, no escalations. |
| 0.90–0.95 | Healthy. Minor issues in one or two dimensions — worth monitoring. |
| 0.80–0.90 | Needs attention. One component is degraded, affecting overall reliability. |
| < 0.80 | Failing silently. Self-improvement loop will trigger on the next `armature improve`. |

---

## Trace capture — the foundation

HQS is meaningless without data. Every stage execution records a `TraceRecord`:

```python
TraceRecord(
    run_id="abc-123",
    workflow_name="compliance-review",
    stage_id="analyst",
    role_type="worker",
    model="claude-haiku-4-5-20251001",
    latency_ms=1842.0,
    success=True,
    output_valid=True,        # matched output_schema
    quorum_score=0.80,        # 4 of 5 workers agreed
    escalation_count=0,       # no on_fail retries needed
    tools_declared=["search_docs"],
    tools_called=["search_docs"],
)
```

Traces accumulate in a SQLite database at `~/.armature/traces.db` (or a path you configure). Each run appends records for every stage. The `compute_hqs` method on `TraceStore` rolls up any set of traces into an `HqsResult` — per-run or across all accumulated runs.

This is the fundamental unit of signal. Without traces, you have a workflow. With traces, you have a system that can reason about itself.

---

## `armature improve` — closing the loop

```bash
armature improve myworkflow.yaml                        # analyze + auto-apply if safe
armature improve myworkflow.yaml --dry-run              # show proposed changes, don't apply
armature improve myworkflow.yaml --target-hqs 0.95      # stricter quality target
armature improve myworkflow.yaml --min-traces 10        # require more evidence before acting
```

The default target is 0.90. The default minimum trace count is 3. You need at least some signal before the optimizer can diagnose anything meaningful.

### The self-improvement loop

```
  accumulated traces
         │
         ▼
  ┌──────────────┐
  │  TraceStore  │  load all traces for this workflow
  │  .query()    │
  └──────┬───────┘
         │
         ▼
  ┌──────────────────┐
  │  compute_hqs()   │  rolling HQS across all runs
  └──────┬───────────┘
         │
         ▼
  ┌─────────────────────┐
  │  DiagnosticAnalyzer │  identifies failure signatures
  │  .analyze()         │  per stage, per run
  └──────┬──────────────┘
         │
    HQS < 0.90
    AND ≥ 3 traces?
         │ yes
         ▼
  ┌──────────────────┐
  │   SpecRefiner    │  LLM call (medium-tier model)
  │   .refine()      │  receives: YAML + diagnostics + HQS breakdown
  └──────┬───────────┘
         │
         ▼
  ┌──────────────────────────────────────┐
  │  _classify_changes()                 │
  │                                      │
  │  safe changes   → auto-apply         │
  │  (description,    overwrite spec     │
  │   on_fail,                           │
  │   model_tier,                        │
  │   timeout_s)                         │
  │                                      │
  │  risky changes  → .pending.yaml      │
  │  (add/remove      human review       │
  │   stages,                            │
  │   output_schema,                     │
  │   safety_rules)                      │
  └──────┬───────────────────────────────┘
         │
         ▼
  ┌─────────────────────────────────────┐
  │  write log → .improve_log.jsonl     │
  │  predictions + verification         │
  │  (always, even if nothing changed)  │
  └─────────────────────────────────────┘
         │
         ▼
    next run accumulates traces
    next `armature improve` verifies
    whether last cycle's predictions
    came true
```

### Failure signatures

`DiagnosticAnalyzer` scans every trace and emits typed `DiagnosticResult` records. There are six failure codes:

| Code | Trigger | What it means |
|------|---------|----------------|
| `stage_failed` | `success=False` | Stage errored or timed out |
| `output_invalid` | `output_valid=False` | Output didn't match declared schema |
| `low_confidence` | `quorum_score < 0.30` on a judge | Workers disagreed sharply; result is unreliable |
| `high_escalation` | `escalation_count >= 2` | Stage needed multiple retries to complete |
| `postcondition_failed` | `error_type == "PostconditionFailed"` | Tool postcondition check failed |
| `low_skill_activation` | tools declared but none called | Role description failed to prompt tool use |

Each diagnostic is tagged with the `stage_id` it came from, so the refiner knows exactly which stage to target. A `low_confidence` on `analyst` is a different problem than a `low_confidence` on `final_judge`, even if the fix looks similar.

### Causal attribution

Inspired by Self-Harness ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1), each `DiagnosticResult` carries a **causal 3-tuple** decomposing the failure into three orthogonal dimensions:

| Field | Question | Example values |
|---|---|---|
| `terminal_cause` | What broke? | `execution_error`, `schema_validation`, `low_confidence`, `postcondition`, `prompt_weak` |
| `causal_status` | Whose fault? | `spec_problem`, `model_problem`, `tool_problem` |
| `mechanism` | How exactly? | `timeout`, `model_underpowered`, `schema_too_strict`, `prompt_missing_instruction` |

A `stage_failed` with `mechanism=timeout` → add `timeout_s`. The same code with `mechanism=runtime_error, causal_status=model_problem` → upgrade the model tier. The surface diagnostic code is identical; the right fix is different.

### Stage leverage

Not all stages matter equally to final HQS. **Stage leverage** measures how predictive each stage is of the run's outcome: for every stage, Armature computes the Pearson correlation `r` between that stage's per-run signal and the run's final HQS.

The per-run stage signal is `mean(quorum_score)` when the stage emits quorum scores (judge / consensus stages), otherwise `mean(success × output_valid)` — a 0/1 proxy for non-judge stages. A stage with high `|r|` is a **leverage stage**: when it goes well, the whole run tends to go well, so improvements there move HQS the most. Stages with near-zero `|r|` are relatively independent noise.

The analysis is gated by a **data-sufficiency guard** so it never over-reads a handful of runs: leverage is reported only once at least `min_runs` (default 8) runs exist *and* at least one stage reaches `|r| ≥ 0.4`. Below that, `armature dashboard` shows "insufficient data" and `armature improve` behaves exactly as before — the feature is dormant until the traces can actually support the claim.

When sufficient, two things change:

- `armature dashboard` renders a **leverage heatmap** panel — one row per stage ranked by `|r|`, with the signal type, the correlation, the run count, and a verdict (`leverage` / `flat` / `noise`).
- `armature improve` **weights proposal coverage by leverage**: a candidate that fixes a high-leverage stage scores higher than one fixing a low-leverage stage, so the optimizer targets the stages that will produce the largest HQS improvement first. (Selection remains latency-aware within the ε-band — leverage reweights coverage, it does not replace the latency tiebreak.)

---

## What SpecRefiner does with the diagnosis

SpecRefiner receives three inputs: the current YAML, the list of failure signatures, and the HQS breakdown. It's a medium-tier LLM call — not frontier. Research ([arXiv:2605.30621](https://arxiv.org/abs/2605.30621)v1) found that medium-tier models achieve equivalent spec-evolution quality to frontier models with at most 3.1 percentage points difference at substantially lower cost. The optimizer uses a cheaper model to make itself better.

The refiner's instructions are specific:

- `output_invalid` on a stage: relax or correct the `output_schema` required fields
- `low_confidence` on a judge stage: enrich the role description with explicit evaluation criteria
- `high_escalation`: increase `on_fail.loop.max` or upgrade `model_tier`
- `stage_failed`: add a `timeout_s` or upgrade `model_tier`
- `low_skill_activation`: rewrite the description to explicitly name tools and when to invoke them

It makes **targeted changes only** — stages performing well are not touched. The revised YAML is a minimal diff from the original, not a rewrite.

### Editable surfaces

You can bound what the refiner is allowed to change with the `self_improvement:` spec field:

```yaml
self_improvement:
  editable_surfaces:
    - descriptions    # role.description text
    - retry_counts    # on_fail.loop.max
    - timeouts        # stage.timeout_s
    # schemas and model_tiers are not listed → locked
```

The default surfaces are `[descriptions, retry_counts, timeouts]`. Locked surfaces are named in the refiner's system prompt so it cannot accidentally modify them. This is adapted from Self-Harness ([arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1), which introduces declared editable sets to bound automated harness evolution.

### Proposal diversity and regression safety

When `n_proposals > 1`, the refiner generates multiple candidates in parallel, each guided by a different diversity hint:

```python
runner = SelfImproveRunner("my_workflow.yml", db, n_proposals=3)
report = await runner.analyze()
print(f"Proposals generated: {report.n_proposals_generated}")
print(f"Regression-risk filtered: {report.regression_risk_count}")
```

The candidate whose `predicted_fixes` most overlap the active diagnostic codes is selected. Before selection, **regression gating** filters out candidates that modify stages with no current diagnostics (healthy stages). If all candidates are risky, the best of the risky set is used — the loop never returns no-proposal due to overly cautious gating. Both counts are written to the JSONL audit log for traceability. Adapted from [arXiv:2606.09498](https://arxiv.org/abs/2606.09498)v1.

### The governance split

Not all proposed changes are equal. The harness classifies every change before applying it:

```
Auto-apply (safe):                 Requires human review:
  role.description changes           adding or removing stages
  on_fail / retry config             output_schema modifications
  model_tier upgrades                safety_rules changes
  timeout_s adjustments
```

Safe changes overwrite the spec file immediately (with a `.orig` backup). Risky changes are written to `myworkflow.pending.yaml` and flagged in the `ImprovementReport` with `requires_review=True`. The workflow continues running the current spec until a human inspects and promotes the pending revision.

---

## Falsifiable predictions

This is the part that separates iteration from cargo-culting.

Every time SpecRefiner proposes a revision, it also declares a **falsifiable contract**: which failure signatures it expects to resolve, and which might temporarily worsen:

```
---PREDICTIONS---
{
  "predicted_fixes": ["output_invalid:analyst", "low_confidence:reviewer"],
  "predicted_regressions": ["high_escalation:reviewer"]
}
```

On the next `armature improve` cycle, the harness verifies these predictions against the new diagnostic state:

- **`verified_fixes`** — predicted to resolve, and they did
- **`missed_predictions`** — predicted to resolve, but still failing
- **`unexpected_regressions`** — new failures that weren't predicted

This accountability record accumulates in the `.improve_log.jsonl` file. A `drift_score` tracks what fraction of current failures had previously been marked as fixed — a rising drift score is a signal that the system is oscillating rather than converging.

The prediction loop prevents the optimizer from making changes that don't actually fix anything. If a refiner consistently misses its predictions or introduces regressions, that's visible in the log and can be surfaced to a human.

---

## The improvement log

Every cycle appends a JSONL entry to `myworkflow.improve_log.jsonl`, whether or not anything changed:

```json
{
  "timestamp": "2025-03-14T09:15:42Z",
  "workflow_name": "compliance-review",
  "n_traces": 47,
  "hqs_before": 0.83,
  "target_hqs": 0.90,
  "needs_improvement": true,
  "applied": true,
  "n_proposals_generated": 3,
  "regression_risk_count": 1,
  "diagnostics": [
    {"code": "output_invalid", "stage_id": "analyst", "details": "output failed schema validation"},
    {"code": "low_confidence", "stage_id": "reviewer", "details": "confidence=0.22"}
  ],
  "predicted_fixes": ["output_invalid:analyst", "low_confidence:reviewer"],
  "predicted_regressions": [],
  "verified_fixes": ["output_invalid:analyst"],
  "missed_predictions": [],
  "unexpected_regressions": [],
  "drift_score": 0.0
}
```

This log is both an audit trail and the input to the next verification cycle. It answers the question every engineer asks when looking at an auto-updated config: *what changed, when, why, and did it work?*

---

## `evaluate:` — stage-level acceptance criteria

HQS measures structural quality across all stages. `evaluate:` criteria measure semantic correctness for individual stages. They're complementary:

```yaml
stages:
  - id: analyst
    role:
      type: worker
      description: |
        Analyze the financial data and return a structured risk assessment.
        Return {"risk_level": "low|medium|high", "evidence": [...], "recommendation": "..."}.
    output_schema:
      required: [risk_level, evidence, recommendation]
    evaluate:
      - "Output contains specific numerical evidence"
      - "Risk level is classified as low, medium, or high"
      - "No recommendations are contradicted by the data"
```

After each run, `EvaluationRunner` calls a language model to score each criterion as pass or fail. These are acceptance tests for the stage — think of them as assertions in a test suite. Failed evaluations show up in the run report and contribute to HQS diagnostics in subsequent cycles.

`evaluate:` criteria are where you encode domain knowledge that can't be captured in a JSON schema. "No recommendations are contradicted by the data" is not something `output_schema` can express. An LLM can assess it.

---

## The compounding effect

Here's why this matters over time:

```
Run 1  → traces → HQS = 0.81 → optimizer proposes: fix analyst output_schema
Run 2  → better traces → HQS = 0.88 → optimizer proposes: enrich reviewer description
Run 3  → better traces → HQS = 0.92 → HQS above target, no changes needed
Run N  → stable → HQS = 0.94 → workflow has converged
```

Each improvement cycle has more evidence than the last. The signal gets cleaner. The fixes get more targeted. Eventually the workflow stabilizes at a high-quality equilibrium — and if something in the environment changes (the model behavior shifts, input data changes shape, a new edge case appears), HQS will drop and the loop will activate again.

This is the fundamental difference between agentic systems that drift and ones that improve. Traditional software has error rates and dashboards. Agentic workflows have HQS and `armature improve`. The mechanism is different; the purpose is the same: **a number that tells you the system is working, and a loop that fixes it when it isn't.**

---

## A complete example: self-improving compliance workflow

```yaml
name: compliance-review
version: "1.0"
mission: "Review documents for regulatory compliance. Be precise and structured."

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  medium:
    provider: anthropic
    model: claude-sonnet-4-6
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: analyst
    role:
      name: ComplianceAnalyst
      type: worker
      model_tier: small
      description: |
        Analyze the following document for regulatory compliance issues.
        Document: {{ doc_content }}
        Return a JSON object with exactly these fields:
          {"issues": [{"clause": "string", "severity": "low|medium|high"}],
           "risk_level": "low|medium|high",
           "requires_escalation": true|false}
    output_schema:
      required: [issues, risk_level, requires_escalation]
    on_fail:
      loop:
        max: 2
    timeout_s: 30
    evaluate:
      - "Each issue identifies a specific regulatory clause by name or number"
      - "Risk level is consistent with the severity of identified issues"
      - "requires_escalation is true only when risk_level is high"

  - id: reviewer
    fan_out: 3                              # run the same review 3 times
    fan_in: consensus                       # take the agreed-upon answer
    partition_source: "[1, 2, 3]"
    role:
      name: ComplianceReviewer
      type: judge
      model_tier: medium
      description: |
        Review the analyst's findings and assess whether the risk classification is correct.
        Analyst output: {{ analyst }}
        Original document: {{ doc_content }}
        Return {"agreed_risk_level": "low|medium|high", "confidence": 0.0-1.0, "notes": "..."}.
    output_schema:
      required: [agreed_risk_level, confidence]
    depends_on: [analyst]
    evaluate:
      - "Confidence is above 0.7 when the risk level is unambiguous"
      - "Notes explain any disagreement with the analyst"

  - id: final_report
    role:
      name: ReportWriter
      type: orchestrator
      model_tier: frontier
      description: |
        Produce a final compliance report from the analyst and reviewer findings.
        Analyst: {{ analyst }}
        Reviewer consensus: {{ reviewer }}
        Return a structured report with executive summary, risk rating, and recommended actions.
    depends_on: [analyst, reviewer]
```

Run this workflow twenty times on a batch of documents. Then:

```bash
armature improve compliance-review.yaml
```

If the analyst stage has been returning invalid output (missing `requires_escalation`), the refiner enriches the description and tightens the schema. If the reviewer quorum score is consistently low (the three workers keep disagreeing), the refiner adds explicit evaluation criteria to the description. The log records what was predicted; the next cycle checks whether it worked.

The workflow author wrote the initial YAML. Armature maintains it.

---

*HQS is the signal. Traces are the evidence. The self-improvement loop is the mechanism. Together they close the gap between "we deployed a workflow" and "we operate a system."*
