# The Judge Pattern in Armature

One LLM evaluates the output of another — quality assurance as a first-class workflow primitive.

---

## Background

The idea that output quality improves when a separate evaluator reviews it is not new. The principle appears across several disciplines: **peer review** in science (no author self-publishes without external scrutiny), **adversarial validation** in security (red teams attack systems their colleagues built), and **ensemble voting** in machine learning (a meta-learner combines or arbitrates between weak learners). Constitutional AI and RLHF both rely on a variant: a model evaluating another model's outputs to generate preference data.

In AI research, the pattern goes by several names — LLM-as-evaluator, LLM-as-judge, model-graded evaluation. The core claim, backed by repeated empirical results, is that a capable model evaluating another model's output correlates well with human judgment, and does so at scale and speed that human review cannot match.

Armature makes this a first-class workflow primitive rather than something you wire up in code around each run.

---

## Why single-pass LLM output is risky

A single LLM call is a sample from a distribution. For many tasks that distribution is well-behaved — the model produces a reliable answer. But for tasks that require nuanced judgment, synthesizing conflicting evidence, or making high-stakes decisions, a single sample carries several failure modes:

**Confident hallucination.** The model produces a fluent, plausible answer that is factually wrong. Without a second evaluator, there is no signal that anything went wrong.

**Scope drift.** A writer stage given a broad prompt may address a slightly different question than the one intended. The output reads well but does not answer the actual objective.

**Uncalibrated confidence.** The model's prose reads as authoritative whether it is 95% sure or 55% sure. Single-pass outputs do not expose this uncertainty.

**Format compliance.** Even with a JSON schema, a model may produce technically valid JSON that violates semantic constraints — a confidence score outside 0–1, a required field with a null value, or an enum value not in the allowed set.

Adding a judge stage inserts a second model call whose sole job is to detect these failure modes before the output leaves the workflow.

---

## Armature's four role types

Armature stages declare a `role.type` that signals intent. There are four:

| Role type | Default model tier | Purpose |
|-----------|-------------------|---------|
| `researcher` | `large` | Gather, search, retrieve |
| `worker` | `small` | Transform, write, extract |
| `orchestrator` | `frontier` | Coordinate, decide, plan |
| `judge` | `frontier` | Evaluate, score, arbitrate |

The tier defaults are not arbitrary. Workers do the bulk of the computation — they run at scale, fan out across many items, and must be cheap. Judges run once (or a few times) and carry decision authority, so they always get your strongest model unless you explicitly override.

Override the defaults with `role_type_defaults` in the spec:

```yaml
role_type_defaults:
  worker: small
  judge: frontier     # default — override only if you have a reason
```

The `type: judge` designation also triggers two harness behaviors:

1. The system prompt prefix for the stage is set to an evaluation-oriented framing: "You are evaluating output quality. Assess carefully, score objectively, and identify specific issues."
2. The engine auto-extracts a `confidence`, `score`, or `quality_score` field from the judge's JSON output and records it as the `quorum_score` on the trace. This feeds HQS metrics, the self-improvement loop, and the bootstrap few-shot selector.

---

## The cost asymmetry

The economics of the judge pattern are favorable because of the asymmetry between workers and judges.

```
                 worker (small)   ~$0.0002 / call
                 worker (small)   ~$0.0002 / call
input ──────►   worker (small)   ~$0.0002 / call  ──► judge (frontier) ~$0.003 / call
                 worker (small)   ~$0.0002 / call
                 worker (small)   ~$0.0002 / call

                 5 workers: ~$0.001                    1 judge: ~$0.003
```

Five small-model workers produce five outputs for roughly one-third the cost of one judge call. The judge reads all five outputs — tokens are cheap on the input side — and issues a verdict. Total cost for the pipeline above is approximately $0.004; without the judge it would be $0.001 but with no quality signal.

At larger fan-out values the asymmetry is more dramatic: 50 worker calls at $0.0002 each cost $0.01; one judge call to evaluate all 50 outputs costs $0.003–$0.005 depending on output length. The judge is the least expensive component in terms of per-call cost, while providing the most value in terms of quality assurance.

---

## Three judge patterns with YAML examples

### 1. Gated pipeline

A judge stage sits downstream of a worker. Its output includes a `verdict` field. A downstream stage uses `skip_if:` to gate on that verdict — expensive processing only runs when quality is sufficient.

```yaml
name: gated-content-pipeline
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: draft
    role:
      name: Writer
      type: worker
      model_tier: small
      description: |
        Write a 500-word product description for: {{ product_name }}
        Requirements: {{ requirements }}
        Return {"content": "..."}
    output_mode: guided_json
    output_schema:
      type: object
      required: [content]
      properties:
        content: { type: string }

  - id: quality_check
    role:
      name: QualityJudge
      type: judge                          # frontier model by default
      description: |
        Evaluate this product description against the stated requirements.
        Product: {{ product_name }}
        Requirements: {{ requirements }}
        Draft: {{ draft.content }}

        Assess:
        - Does it satisfy every stated requirement?
        - Is the tone appropriate for the audience?
        - Are there factual claims that cannot be verified?
        - Is it the right length and structure?

        Return:
          {
            "verdict": "APPROVED" | "REVISE" | "REJECT",
            "score": 0.0,
            "issues": ["specific issue 1", "specific issue 2"],
            "notes": "one-sentence summary"
          }
    output_mode: guided_json
    output_schema:
      type: object
      required: [verdict, score]
      properties:
        verdict: { type: string, enum: [APPROVED, REVISE, REJECT] }
        score:   { type: number, minimum: 0.0, maximum: 1.0 }
        issues:
          type: array
          items: { type: string }
        notes: { type: string }
    depends_on: [draft]

  - id: publish
    skip_if: "{{ quality_check.verdict != 'APPROVED' }}"   # gate: only runs on APPROVED
    role:
      name: Publisher
      type: worker
      model_tier: small
      description: |
        Format this approved description for publication.
        Content: {{ draft.content }}
        Return {"formatted": "..."}
    depends_on: [quality_check]

  - id: revision_handler
    skip_if: "{{ quality_check.verdict == 'APPROVED' }}"   # only runs when NOT approved
    role:
      name: RevisionPlanner
      type: orchestrator
      model_tier: frontier
      description: |
        The draft was {{ quality_check.verdict }}.
        Issues: {{ quality_check.issues }}
        Draft: {{ draft.content }}
        Produce a revision plan with specific edits required.
        Return {"revision_plan": "...", "priority_fixes": [...]}
    depends_on: [quality_check]
```

The judge sits between the cheap worker and the expensive publish stage. If quality is insufficient, the publish stage is skipped entirely — no downstream API call, no database write, no human notification. The revision path fires instead.

---

### 2. Consensus judge — running the same prompt N times

For high-stakes classification or decisions where model uncertainty is a concern, run the same prompt multiple times and let the harness aggregate results. `fan_in: consensus` collects all N outputs and calls a synthesizing LLM to pick the most-agreed-upon answer.

```yaml
name: consensus-sentiment
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: classify
    fan_out: 5
    fan_in: consensus
    partition_source: "[1, 2, 3, 4, 5]"    # five identical runs
    role:
      name: SentimentJudge
      type: judge
      model_tier: small                    # cheap per-run; quality comes from N runs
      description: |
        Classify the sentiment of the following customer message.
        Message: {{ customer_message }}

        Return ONLY:
        {
          "sentiment": "positive" | "neutral" | "negative",
          "confidence": 0.0,
          "reasoning": "one sentence"
        }
    output_mode: guided_json
    output_schema:
      type: object
      required: [sentiment, confidence]
      properties:
        sentiment:  { type: string, enum: [positive, neutral, negative] }
        confidence: { type: number, minimum: 0.0, maximum: 1.0 }
        reasoning:  { type: string }

  - id: route
    role:
      name: Router
      type: orchestrator
      description: |
        Classification result: {{ classify.sentiment }} (confidence: {{ classify.confidence }})
        Route this customer message accordingly.
        Return {"queue": "priority" | "standard" | "review", "reason": "..."}
    depends_on: [classify]
```

When all five runs agree on `negative`, you have strong signal. When they split three-to-two, the model is uncertain on this input — `classify.confidence` will be lower, and the `route` stage can direct the message to a human review queue instead.

The `fan_in: consensus` strategy handles the aggregation: the harness collects all five result dicts and calls a synthesizing LLM to produce a single coherent output. If you prefer to aggregate manually, use `fan_in: list` and write a downstream judge stage that receives the list of five results and reasons about the distribution itself.

---

### 3. Escalation gate — retry until a judge-defined condition is satisfied

`on_fail.loop` combined with an `until:` condition creates a retry loop that runs until the judge declares success. The judge's output fields are available in the `until` expression directly.

```yaml
name: quality-assured-summary
version: "1.0"

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  - id: summarize
    role:
      name: Summarizer
      type: worker
      model_tier: small
      description: |
        Summarize the following document in 150–200 words.
        Document: {{ document }}

        {% if _retry_attempt %}
        PREVIOUS ATTEMPT SCORED {{ _last_result.score | default('?') }}/1.0
        Judge feedback: {{ _last_result.feedback | default('none') }}
        Revise based on that feedback.
        {% endif %}

        Return {"summary": "..."}
    output_mode: guided_json
    output_schema:
      type: object
      required: [summary]
      properties:
        summary: { type: string }
    on_fail:
      loop:
        stage: judge_summary    # loop back to the judge after each revision
        max: 3                  # at most 3 retries (4 total attempts)
        until: "{{ score >= 0.85 }}"
        backoff_s: 0.5

  - id: judge_summary
    role:
      name: QualityJudge
      type: judge
      description: |
        Score this summary of the original document.
        Original: {{ document }}
        Summary: {{ summarize.summary }}

        Criteria:
        - All key facts from the original are present
        - No information was added that is not in the source
        - Length is 150–200 words
        - Prose is clear and professional

        Return:
          {
            "score": 0.0,
            "passed": true | false,
            "feedback": "specific instructions for improvement if score < 0.85"
          }
    output_mode: guided_json
    output_schema:
      type: object
      required: [score, passed]
      properties:
        score:    { type: number, minimum: 0.0, maximum: 1.0 }
        passed:   { type: boolean }
        feedback: { type: string }
    depends_on: [summarize]
```

The loop runs: `summarize → judge_summary`. If `score >= 0.85`, the `until` condition is satisfied and the loop exits. If not, the harness retries `summarize` with `_retry_attempt` and `_last_result` (the judge's previous output, including `feedback`) injected into context. The worker can read the judge's feedback and revise accordingly. After at most four total attempts, the loop exits with the best result obtained.

---

## Composing judges with fan-out

The natural extension of the judge pattern is reviewing a batch of worker outputs. Fan-out produces a list; a judge stage reads the full list.

```
                   ┌─── worker (item 1) ───┐
                   │                       │
documents ────────►├─── worker (item 2) ───┤──► list ──► judge ──► summary report
                   │                       │
                   └─── worker (item N) ───┘
                       (concurrent, bounded
                        by fan_out limit)
```

```yaml
stages:
  - id: review_each
    fan_out: 10
    fan_in: list
    partition_source: "{{ documents }}"
    partition_key: doc_path
    inject_file_as: doc_content
    role:
      name: Reviewer
      type: worker
      model_tier: small
      description: |
        Review this document for compliance issues.
        Document: {{ doc_path }}
        Content: {{ doc_content }}
        Return:
          {
            "issues": [{"clause": "...", "severity": "low|medium|high"}],
            "risk_level": "low|medium|high",
            "requires_escalation": true|false
          }

  - id: escalation_judge
    skip_if: >-
      {{ review_each | selectattr('requires_escalation') | list | length == 0 }}
    role:
      name: EscalationJudge
      type: judge                  # frontier model reviews the flagged subset
      description: |
        {{ review_each | selectattr('requires_escalation') | list | length }} documents
        require escalation review. Assess each and produce a prioritized escalation report.

        Flagged reviews:
        {{ review_each | selectattr('requires_escalation') | list | tojson }}

        For each, assess:
        - Is escalation genuinely warranted (not a false positive)?
        - What is the business impact?
        - What remediation is required?

        Return:
          {
            "escalations": [
              {"doc": "...", "confirmed": true|false, "impact": "...", "action": "..."}
            ],
            "summary": "..."
          }
    depends_on: [review_each]

  - id: final_report
    role:
      name: ReportWriter
      type: orchestrator
      model_tier: frontier
      description: |
        Produce the final compliance report from {{ review_each | length }} document reviews.
        All reviews: {{ review_each }}
        Escalations (if any): {{ escalation_judge | default({}) }}
    depends_on: [review_each, escalation_judge]
```

The judge is skipped entirely when no documents flag for escalation — `skip_if:` short-circuits it. When there are escalations, the judge reads only the flagged subset, not all N reviews. This is the correct scoping: do not pass noise to the judge when the relevant signal is a subset.

Per-item fan-out errors (`{"_fan_out_error": "..."}`) should be filtered before passing to the judge to avoid confusing the evaluator with error objects:

```yaml
description: |
  Successful reviews: {{ review_each | rejectattr('_fan_out_error') | list | tojson }}
  Failed items: {{ review_each | selectattr('_fan_out_error') | list | length }} (excluded)
```

---

## Post-run declarative evaluation

For any stage — not just judge-typed stages — the `evaluate:` field declares quality criteria that the `EvaluationRunner` scores automatically after the workflow completes. You do not need a separate judge stage for this; the harness runs an LLM evaluator against the stage's recorded trace output.

```yaml
stages:
  - id: writer
    role:
      name: Writer
      type: worker
      model_tier: small
      description: |
        Write a product announcement for {{ product_name }}.
    evaluate:
      - "The announcement mentions the product name at least once"
      - "No pricing information is included (pricing is set separately)"
      - "Tone is professional and positive, not promotional or hyperbolic"
      - "Length is between 100 and 300 words"
```

The `EvaluationRunner` reads the stage's output from the trace store after the run, calls an LLM evaluator with each criterion, and records a `score` (0.0–1.0), `criteria_passed`, `criteria_failed`, and `notes` in the evaluation store. Results appear in `armature report`.

This differs from a judge stage: `evaluate:` criteria are assessed post-run, do not block or branch the workflow, and do not produce output that downstream stages can read. Use `evaluate:` for ongoing quality monitoring and trend analysis across many runs. Use a judge stage when the evaluation result needs to influence the workflow at runtime — gating, routing, or triggering a retry.

---

## When not to use a judge

The judge pattern adds latency (one more LLM call), cost (frontier model tier), and complexity (another stage to prompt and tune). It is not always the right tool.

**Skip the judge when:**

- The output is deterministic or near-deterministic. A stage that formats a date, converts units, or executes a SQL query does not benefit from evaluation. The correctness check is structural, not semantic.

- The downstream consumer can validate. If a downstream stage will raise an error on bad input — a JSON parser, a schema validator, a database write with constraints — the failure mode is already handled. Adding a judge duplicates the check.

- The cost of a wrong output is low and easily corrected. Internal tooling, draft content for human review, and exploratory analysis pipelines may not justify judge overhead.

- You are in early development. A judge is most valuable when you have a stable prompt and want to catch regressions. During rapid iteration, judge criteria become stale quickly and add noise rather than signal.

**Use a judge when:**

- The output is a high-stakes decision, recommendation, or public-facing content where quality matters and errors are costly.
- The worker stage uses a small model and you cannot guarantee output quality without external validation.
- You need a calibrated confidence signal (`quorum_score`) for the self-improvement loop, training data export, or escalation routing.
- You are running at scale (many fan-out items) and need to spot systematic failures before they reach downstream systems.

---

## Summary

```
Single-pass risk:     worker ──────────────────────────────► output (no quality signal)

Judge pattern:        worker ──► judge ──► output           (quality signal, gating)

Consensus pattern:    worker ×N ──► consensus ──► output    (uncertainty signal)

Escalation pattern:   worker ──► judge ──┐                  (loop until quality met)
                          ▲              │
                          └──────────────┘ (retry with feedback)

Fan-out + judge:      workers ×N ──► list ──► judge ──► report (batch review)
```

The `type: judge` designation in a stage spec is a single field. The harness handles the frontier model selection, the system prompt framing, the quorum score extraction, and the trace recording. The workflow author writes the evaluation criteria in plain language in the role description. The pattern scales from a two-stage pipeline with one critic to a hundred-document compliance pipeline with an escalation judge — the YAML structure is the same in both cases.

---

*Armature — the harness is more important than the model.*
