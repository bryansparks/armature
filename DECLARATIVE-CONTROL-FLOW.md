# Declarative Control Flow in Armature

Dependency ordering, conditional skipping, retry loops, and timeout guards — all expressed in YAML, none written in Python.

---

If you have written a GitHub Actions workflow or a Kubernetes CronJob, you already understand the core idea: instead of writing `if` statements, `try/except` blocks, and `while` loops in application code, you declare your intent in structured configuration. The runtime reads that declaration and handles the mechanics.

Armature applies the same principle to LLM calls. The fields `depends_on`, `skip_if`, `condition`, `on_fail.loop`, `timeout_s`, and `fail_as_value` are the complete vocabulary for controlling when a stage runs, how it behaves under failure, and when to give up. A workflow author who has never written `asyncio.gather` can express parallel fan-out, conditional skipping, and exponential-backoff retry in a single spec file — and a teammate who has never seen the source code can read it and understand what the workflow does.

---

## `depends_on` — ordering and parallelism

`depends_on` is a list of stage IDs that must complete before this stage is eligible to run. The DAGExecutor uses Kahn's topological sort to compute the full execution graph from these declarations, then `asyncio.gather` to run each "ready wave" concurrently.

A **wave** is the set of stages whose every dependency has already completed. The executor fires all stages in a wave simultaneously — no extra code, no semaphores, no `asyncio.create_task` calls in the spec. When the wave finishes, results merge into the shared context dict and the next wave is computed.

```
               Wave 1              Wave 2         Wave 3
            ┌────────────┐      ┌──────────┐   ┌──────────┐
  start ───►│ fetch_data │─────►│ analyse  │──►│  report  │
            └────────────┘  ┌──►└──────────┘   └──────────┘
            ┌─────────────┐ │                        ▲
  start ───►│ validate_   │─┘  ┌──────────┐          │
            │ schema      │───►│ summarise│──────────┘
            └─────────────┘    └──────────┘
                             (concurrent)
```

```yaml
stages:
  - id: fetch_data
    role:
      name: DataFetcher
      type: researcher
      description: Retrieve the raw dataset. Return {"records": [...]}.

  - id: validate_schema
    role:
      name: Validator
      type: worker
      description: Confirm the dataset matches the expected schema. Return {"valid": true}.

  - id: analyse
    depends_on: [fetch_data, validate_schema]   # waits for both
    role:
      name: Analyst
      type: worker
      description: |
        Analyse {{ fetch_data.records | length }} records for anomalies.
        Schema valid: {{ validate_schema.valid }}
        Return {"anomalies": [...], "anomaly_rate": 0.0}.

  - id: summarise
    depends_on: [fetch_data]                    # depends on fetch only — runs in wave 2
    role:                                       # runs concurrently with analyse
      name: Summariser
      type: worker
      description: Produce a one-paragraph summary of the dataset.

  - id: report
    depends_on: [analyse, summarise]            # waits for both wave-2 stages
    role:
      name: ReportWriter
      type: orchestrator
      description: |
        Combine the anomaly analysis and dataset summary into a final report.
        Anomalies: {{ analyse.anomalies }}
        Summary: {{ summarise }}
```

Stages with no `depends_on` run in wave 1 (simultaneously). `fetch_data` and `validate_schema` both have no dependencies, so they start together. When both finish, `analyse` and `summarise` become ready — they run together in wave 2. `report` fires in wave 3 only when both wave-2 stages complete.

The DAG is a consequence of the spec, not something you wire up. The executor derives it automatically.

**What happens with a cycle?** The executor raises `ValueError: DAG has a cycle` at startup, before any LLM call is made. Cycles are structurally impossible to run and are rejected immediately.

---

## `skip_if` — negative conditional gate

`skip_if` is a Jinja2 expression. When it renders to `"true"`, `"1"`, or `"yes"` (case-insensitive), the stage is skipped entirely — returning `{"_skipped": True}` into the context without making any LLM call.

This is the escape hatch for stages that are only relevant when certain conditions hold. A compliance escalation stage, for example, should not run if nothing was flagged:

```yaml
  - id: escalation_review
    depends_on: [document_reviews]
    skip_if: >-
      {{ document_reviews
         | selectattr('requires_escalation')
         | list | length == 0 }}
    role:
      name: EscalationJudge
      type: judge
      model_tier: frontier
      description: |
        {{ document_reviews | selectattr('requires_escalation') | list | length }}
        documents require senior review. Assess each flagged issue and produce
        an escalation dossier. Flagged: {{ document_reviews | selectattr('requires_escalation') | list }}
```

If no document has `requires_escalation: true`, the entire stage — including its frontier-model call — is skipped. Downstream stages see `escalation_review._skipped == True` and can filter accordingly:

```yaml
  - id: final_report
    depends_on: [document_reviews, escalation_review]
    role:
      description: |
        {% if escalation_review._skipped %}
        No items required escalation. Produce a clean-pass report.
        {% else %}
        Escalation findings: {{ escalation_review }}
        Produce a report incorporating the escalation dossier.
        {% endif %}
```

`skip_if` is evaluated before any LLM call, so skipped stages have zero cost and zero latency.

---

## `condition` — positive conditional gate

`condition` is the logical inverse of `skip_if`. Instead of "skip when X is true," it means "run only when X is true." When the Jinja2 expression renders to anything other than `"true"`, `"1"`, or `"yes"`, the stage is skipped (same `{"_skipped": True}` return).

```yaml
  - id: high_risk_deep_dive
    depends_on: [risk_assessment]
    condition: "{{ risk_assessment.risk_level == 'high' }}"
    role:
      name: RiskAnalyst
      type: judge
      model_tier: frontier
      description: |
        The risk assessment flagged this as HIGH risk (score: {{ risk_assessment.score }}).
        Perform a detailed root-cause analysis and mitigation plan.
```

The choice between `skip_if` and `condition` is a matter of readability. Use `skip_if` when skipping is the natural framing ("skip the escalation if there's nothing to escalate"). Use `condition` when running is the natural framing ("run the deep dive only when risk is high"). Both are equivalent in implementation — they share the same evaluation path and both return `{"_skipped": True}` when inactive.

Neither field requires the other. You can use both on the same stage — the stage is skipped if either triggers.

---

## `on_fail.loop` — retry with context feedback

`on_fail.loop` declares a retry policy that activates when a stage fails or when an `until` condition is not satisfied. The full configuration (from `spec/models.py`):

```python
class LoopConfig(BaseModel):
    stage: str                      # stage to retry (almost always self)
    context: str = "retry"          # label for this retry context
    max: int = 3                    # maximum retry attempts
    until: str | None = None        # Jinja2 expr; stop retrying when truthy
    backoff_s: float | None = None  # initial wait (seconds); doubles each attempt
    backoff_max_s: float = 60.0     # ceiling on per-attempt backoff
```

### Basic retry on failure

```yaml
  - id: parse_invoice
    on_fail:
      loop:
        stage: parse_invoice
        max: 3
    role:
      name: InvoiceParser
      type: worker
      description: |
        Parse this invoice PDF and extract all line items.
        {% if _retry_attempt is defined %}
        Previous attempt failed: {{ _last_error }}
        Try a different extraction approach.
        {% endif %}
        Return {"line_items": [...], "total": 0.00}.
```

On each retry, the harness injects three context variables that the stage's role description can reference directly:

| Variable | Set when | Value |
|----------|----------|-------|
| `_retry_attempt` | Always on retry | `1`, `2`, `3` — current attempt number |
| `_last_error` | Previous attempt threw an exception | The exception message as a string |
| `_last_result` | Previous attempt succeeded but `until` not satisfied | The full result dict from the previous run |

This is the key difference from a simple HTTP retry. The LLM sees what it produced on the previous attempt and can correct it — it is not repeating the same call, it is learning from its own output.

### Retry until a quality threshold is met

`until` is a Jinja2 expression evaluated against the stage result. Retrying continues until the expression is truthy or `max` attempts are exhausted.

```yaml
  - id: draft_executive_summary
    on_fail:
      loop:
        stage: draft_executive_summary
        max: 4
        until: "{{ word_count >= 200 and word_count <= 300 }}"
    role:
      name: Writer
      type: worker
      description: |
        Write an executive summary of these findings.
        Target length: 200-300 words.
        {% if _last_result is defined %}
        Your previous draft had {{ _last_result.word_count }} words.
        Adjust accordingly: {{ _last_result.summary }}
        {% endif %}
        Return {"summary": "...", "word_count": 0}.
```

When the previous attempt succeeded but `until` was not satisfied, `_last_result` carries the full result dict. The model sees its own output and understands precisely what to fix.

### Exponential backoff

```yaml
  - id: call_external_api
    on_fail:
      loop:
        stage: call_external_api
        max: 5
        backoff_s: 1.0        # wait 1s before attempt 2
        backoff_max_s: 30.0   # cap at 30s
    tool_call:
      name: http_get
      args:
        url: "https://api.example.com/data"
```

Backoff schedule for `backoff_s: 1.0`:

| Attempt | Wait before next |
|---------|-----------------|
| 1 (initial) | — |
| 2 | 1s |
| 3 | 2s |
| 4 | 4s |
| 5 | 8s (capped at backoff_max_s if lower) |

### What never retries: `ToolBlocked`

When a safety rule blocks a tool call, the harness raises `ToolBlocked` and stops immediately — retrying a blocked tool call cannot produce a different outcome. The `ToolBlocked` exception bypasses the retry loop entirely and propagates as a stage failure. This is enforced in the engine: `if isinstance(exc, ToolBlocked): raise` before any backoff logic runs.

---

## `timeout_s` — wall-clock limit

`timeout_s` sets a wall-clock deadline in seconds for the entire stage including all retries. When exceeded, `asyncio.wait_for` raises `TimeoutError`, which propagates as a stage failure.

```yaml
  - id: long_running_analysis
    timeout_s: 120.0           # 2-minute limit including all retries
    on_fail:
      loop:
        stage: long_running_analysis
        max: 3
        backoff_s: 5.0
    role:
      name: DeepAnalyst
      type: worker
      description: Perform deep structural analysis of the codebase.
```

`timeout_s` applies to the complete stage lifecycle — initial attempt plus all retries — not to each individual attempt. If three retries each take 50 seconds, the timeout fires at 120 seconds and the stage fails even if a fourth attempt might have succeeded.

---

## `fail_as_value` — graceful failure capture

By default, a stage failure raises an exception and aborts the workflow. `fail_as_value: true` changes this: the stage returns a structured failure dict instead of raising, allowing downstream stages to handle it explicitly.

```yaml
  - id: optional_enrichment
    fail_as_value: true
    timeout_s: 30.0
    tool_call:
      name: http_get
      args:
        url: "{{ enrichment_api_url }}"

  - id: final_output
    depends_on: [optional_enrichment]
    role:
      description: |
        {% if optional_enrichment._failed %}
        Enrichment data unavailable ({{ optional_enrichment._failed_reason }}).
        Proceed with base data only.
        {% else %}
        Enrichment data: {{ optional_enrichment }}
        {% endif %}
```

The failure dict structure:

```json
{
  "_failed": true,
  "_failed_reason": "timeout after 30.0s",
  "_failed_type": "TimeoutError"
}
```

`fail_as_value` pairs naturally with `timeout_s` for optional external calls, and with `on_fail.loop` for cases where you want to exhaust retries and then continue rather than abort.

---

## The GitHub Actions analogy

These fields will feel familiar to anyone who has written CI/CD YAML:

| Armature field | GitHub Actions equivalent | Key difference |
|----------------|--------------------------|----------------|
| `depends_on: [a, b]` | `needs: [a, b]` on a job | Identical semantics; Armature infers the DAG, GHA requires it per-job |
| `skip_if: "{{ ... }}"` | `if: ${{ ... == false }}` on a step | Same evaluation model; Armature uses Jinja2, GHA uses expression syntax |
| `condition: "{{ ... }}"` | `if: ${{ ... }}` on a step | Positive vs. negative framing of the same gate |
| `on_fail.loop` with `max: 3` | `retry: max-attempts: 3` (third-party action) | Armature adds backoff, `until` conditions, and LLM feedback via `_last_result` |
| `timeout_s: 120` | `timeout-minutes: 2` | Same concept; Armature applies per-stage, GHA applies per-job |
| `fail_as_value: true` | `continue-on-error: true` | Armature provides a structured failure dict; GHA just continues |

The critical distinction is what is being controlled. In GitHub Actions, these fields govern shell commands. In Armature, they govern LLM calls — and retrying with feedback in context is genuinely useful in ways that retrying `npm install` is not. The model can see `{{ _last_result }}` and understand what it produced on the previous attempt.

---

## Why declaring control flow is better than coding it

### Readability at a glance

Compare the Python version of a conditional skip:

```python
if len([r for r in review_results if r.get("requires_escalation")]) == 0:
    results["escalation_review"] = {"_skipped": True}
else:
    results["escalation_review"] = await escalation_judge(context)
```

...to the YAML version:

```yaml
skip_if: >-
  {{ review_results | selectattr('requires_escalation') | list | length == 0 }}
```

The Jinja2 expression is not shorter, but its context is unambiguous: it lives on a stage declaration, not buried in orchestration code. Someone reading the spec sees the skip condition directly on the stage it affects.

### Non-engineer accessibility

A product manager, data scientist, or domain expert can read a YAML spec and understand the workflow. They can modify `max: 3` to `max: 5`, add a `timeout_s: 60`, or adjust a `condition` expression without knowing anything about `asyncio`, Pydantic models, or how the DAG executor works. The control flow is legible at the layer where the workflow logic lives.

This matters operationally. Workflows that require a Python developer to modify will not be tuned often. Workflows expressed in YAML can be iterated by the people who understand the domain.

### Version-controlled intent

When `max: 3` changes to `max: 5`, the git diff shows exactly what changed and why — a commit message reading "increase retry limit after observing JSON parse failures in production" is sufficient documentation. When the same change lives inside a Python method, the diff shows a number changing with no surrounding context about what the retry governs.

Similarly, `skip_if` makes the skip condition an explicit, named thing in the spec. When a condition needs to change — because the business rule changed — the change is a one-line diff in a YAML file, reviewed in a pull request, with a clear history of when it changed and who approved it.

### The harness enforces it

Declared control flow is not just convention — it is enforced by the engine. The DAGExecutor raises at startup if `depends_on` creates a cycle. `skip_if` and `condition` expressions are evaluated before any LLM call is dispatched. `timeout_s` is enforced by `asyncio.wait_for`, not by hope. `ToolBlocked` never retries. The harness provides these guarantees whether or not the workflow author thought to implement them.

When control flow lives in application code, the author must remember to add timeout handling, to skip the right stages, to inject retry context. In Armature, they declare what they want and the engine provides it.

---

## Complete example: all four mechanisms together

A content moderation pipeline that fetches items, reviews them in parallel, retries unclear cases with feedback, skips the escalation queue when nothing is serious, and times out any stage that runs too long.

```yaml
name: content-moderation
version: "1.0"
mission: >
  Review submitted content for policy violations.
  Escalate borderline cases to senior review.
  All decisions must be explainable and auditable.

model_tiers:
  small:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  frontier:
    provider: anthropic
    model: claude-opus-4-7

stages:
  # Wave 1: fetch the submission queue and load policy docs in parallel
  - id: fetch_submissions
    timeout_s: 30.0
    fail_as_value: true          # don't abort if the queue API is down
    tool_call:
      name: http_get
      args:
        url: "https://queue.example.com/pending"

  - id: load_policy
    timeout_s: 15.0
    tool_call:
      name: read_file
      args:
        path: "/policies/content_policy_v3.md"

  # Wave 2: fan-out review of each submission — depends_on both wave-1 stages
  - id: review_submissions
    depends_on: [fetch_submissions, load_policy]
    skip_if: "{{ fetch_submissions._failed }}"   # nothing to review if queue is down
    fan_out: 8
    fan_in: list
    partition_source: "{{ fetch_submissions.items }}"
    partition_key: submission
    timeout_s: 45.0
    on_fail:
      loop:
        stage: review_submissions
        max: 2
        until: "{{ verdict in ('approved', 'rejected', 'escalate') }}"
        backoff_s: 2.0
    role:
      name: ContentReviewer
      type: worker
      model_tier: small
      description: |
        Review this submission against the content policy.
        Policy: {{ load_policy.content }}
        Submission: {{ submission.text }}

        {% if _last_result is defined %}
        Your previous assessment was ambiguous (verdict: {{ _last_result.verdict }}).
        The verdict must be one of: approved, rejected, or escalate.
        Reconsider with that constraint.
        Previous reasoning: {{ _last_result.reasoning }}
        {% endif %}

        Return:
          {
            "submission_id": "...",
            "verdict": "approved|rejected|escalate",
            "reasoning": "...",
            "confidence": 0.0
          }

  # Wave 3a: escalation review — skipped entirely if nothing needs senior review
  - id: senior_review
    depends_on: [review_submissions]
    skip_if: >-
      {{ review_submissions
         | rejectattr('_skipped', 'defined')
         | selectattr('verdict', 'equalto', 'escalate')
         | list | length == 0 }}
    timeout_s: 120.0
    on_fail:
      loop:
        stage: senior_review
        max: 3
        backoff_s: 5.0
        backoff_max_s: 30.0
    role:
      name: SeniorModerator
      type: judge
      model_tier: frontier
      description: |
        {{ review_submissions
           | selectattr('verdict', 'equalto', 'escalate')
           | list | length }} submissions require senior review.

        Escalated items:
        {{ review_submissions | selectattr('verdict', 'equalto', 'escalate') | list }}

        {% if _retry_attempt is defined %}
        Retry {{ _retry_attempt }}/3. Previous attempt error: {{ _last_error }}
        {% endif %}

        For each item, produce a final binding verdict with a written rationale.
        Return {"escalation_decisions": [{"submission_id": "...", "final_verdict": "...", "rationale": "..."}]}

  # Wave 3b: metrics aggregation — runs concurrently with senior_review
  - id: compute_metrics
    depends_on: [review_submissions]
    condition: "{{ review_submissions | rejectattr('_fan_out_error', 'defined') | list | length > 0 }}"
    role:
      name: MetricsAggregator
      type: worker
      model_tier: small
      description: |
        Compute moderation metrics from {{ review_submissions | length }} review results.
        Successful reviews: {{ review_submissions | rejectattr('_fan_out_error', 'defined') | list }}
        Return {"approved_rate": 0.0, "rejection_rate": 0.0, "escalation_rate": 0.0, "avg_confidence": 0.0}.

  # Wave 4: final report — waits for both wave-3 stages
  - id: moderation_report
    depends_on: [senior_review, compute_metrics]
    timeout_s: 60.0
    role:
      name: ReportWriter
      type: orchestrator
      model_tier: frontier
      description: |
        Produce the final moderation session report.

        Review summary:
          Total submissions: {{ review_submissions | length }}
          Approved: {{ review_submissions | selectattr('verdict', 'equalto', 'approved') | list | length }}
          Rejected: {{ review_submissions | selectattr('verdict', 'equalto', 'rejected') | list | length }}
          Escalated: {{ review_submissions | selectattr('verdict', 'equalto', 'escalate') | list | length }}

        {% if not senior_review._skipped %}
        Senior review decisions: {{ senior_review.escalation_decisions }}
        {% endif %}

        {% if not compute_metrics._skipped %}
        Session metrics: {{ compute_metrics }}
        {% endif %}

        Return a structured JSON report suitable for the audit log.
```

What this spec expresses without a single line of Python orchestration code:

- `fetch_submissions` and `load_policy` run in parallel (wave 1), with independent timeouts
- If the queue API fails, `review_submissions` is skipped cleanly rather than crashing
- Each submission is reviewed concurrently (up to 8 at a time), retrying with its own output as feedback if the verdict is ambiguous
- `senior_review` and `compute_metrics` run in parallel (wave 3), but senior review is skipped if nothing was flagged — saving a frontier-model call on clean sessions
- `compute_metrics` only runs if at least one review succeeded
- `senior_review` retries up to 3 times with exponential backoff if it fails
- The final report waits for both wave-3 stages and incorporates whichever ran

---

*Armature — the harness is more important than the model.*
