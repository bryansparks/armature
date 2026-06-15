# Quorum Scoring in Armature

A judge's self-reported confidence — per-stage signal for HQS and the self-improvement loop.

---

Quorum score is a number between 0.0 and 1.0 that a judge stage emits alongside its primary output. It answers the question: *how confident is this judge in what it just said?* A score of 0.95 means the evaluation is well-supported and the output can be trusted. A score of 0.5 means the judge is effectively guessing — the input is ambiguous, the evidence is thin, or the criteria are insufficiently defined.

The harness extracts this number automatically from the judge's output and records it as a field on the `TraceRecord`. It flows into HQS, into the `DiagnosticAnalyzer`, and — when low enough — into the self-improvement loop. The workflow author does not wire any of this up; declaring `type: judge` and returning a `confidence` field is sufficient.

---

## How the harness extracts it

The harness looks for the quorum score in the judge's JSON output by checking three keys in priority order:

```
"score"  →  "quality_score"  →  "confidence"
```

The first key found whose value is a number in the range `[0.0, 1.0]` becomes the quorum score. Keys outside that range, non-numeric values, and missing keys are all silently ignored — the quorum score for that trace is recorded as `None`.

Only `type: judge` stages participate. Any other role type — `worker`, `researcher`, `orchestrator` — returns `None` unconditionally and does not contribute a quorum score to the trace, regardless of what fields appear in its output.

The extraction in the harness:

```python
_QUORUM_SCORE_KEYS = ("score", "quality_score", "confidence")

def _extract_quorum_score(role_type: str, result: dict) -> float | None:
    """Only judge stages produce quorum scores; all other role types return None."""
    if role_type != "judge":
        return None
    for key in _QUORUM_SCORE_KEYS:
        val = result.get(key)
        if isinstance(val, (int, float)) and 0.0 <= float(val) <= 1.0:
            return float(val)
    return None
```

A judge that returns `{"risk_level": "high", "confidence": 0.92}` produces a quorum score of `0.92`. A judge that returns `{"verdict": "APPROVED", "score": 0.88}` produces a quorum score of `0.88`. A judge that returns `{"passed": true}` with no recognized numeric field produces no quorum score — the trace records `None`.

---

## Declaring a quorum-scored judge

The judge's role description should tell the model what to put in the confidence field and how to calibrate it. Vague instructions produce uncalibrated confidence scores. Explicit instructions produce scores the self-improvement loop can act on.

```yaml
stages:
  - id: risk_judge
    role:
      name: RiskAssessor
      type: judge
      model_tier: frontier
      description: |
        Evaluate the risk level of this contract.

        Contract text: {{ analyst.contract_text }}
        Extracted clauses: {{ analyst.clauses }}

        Return:
        {
          "risk_level": "low|medium|high|critical",
          "confidence": 0.0-1.0,
          "rationale": "..."
        }

        Calibration:
        - Set confidence to 0.9+ when the risk level is clearly supported by specific clauses.
        - Set confidence to 0.7–0.89 when the evidence is present but interpretation is required.
        - Set confidence to 0.5–0.69 when key information is missing or ambiguous.
        - Set confidence to 0.5 when you cannot determine the risk level from available evidence.
    output_schema:
      required: [risk_level, confidence]
    depends_on: [analyst]
```

The calibration guidance is not decoration — it teaches the judge how to distinguish between "I am sure" and "I am guessing." Without it, frontier models tend to emit high confidence regardless of evidence quality, which defeats the purpose of the signal.

---

## The connection to HQS

HQS is a weighted composite of five signals derived from trace data:

```
HQS = 0.35 × output_valid_rate
    + 0.25 × success_rate
    + 0.20 × avg_quorum_score
    + 0.10 × latency_score
    + 0.10 × happy_path_rate
```

`avg_quorum_score` is the mean quorum score across all judge stage traces for the workflow. Stages without a quorum score (non-judge stages, or judge stages that returned no recognized confidence field) contribute `0.5` to this average — the noise floor, treated as neither confident nor completely uncertain.

A workflow with three judge stages that consistently score `0.9`, `0.85`, and `0.88` contributes roughly `0.88` to the `avg_quorum_score` component — a healthy contribution to HQS. A workflow whose judges consistently score `0.5–0.55` is contributing near-zero signal quality on a component that carries 20% of the total weight.

Quorum score is a per-run, per-stage value. `avg_quorum_score` is the aggregate across all judge traces accumulated for the workflow. A single ambiguous run does not drag HQS down; a pattern of low-confidence runs does.

---

## The self-improvement loop connection

When the `DiagnosticAnalyzer` processes accumulated traces, it emits a `LOW_CONFIDENCE` diagnostic for any judge stage whose quorum score falls below the threshold:

| Diagnostic code | Trigger condition |
|----------------|------------------|
| `low_confidence` | `quorum_score < 0.30` on a judge stage trace |

A `LOW_CONFIDENCE` on a specific stage tells the `SpecRefiner` exactly what to fix: enrich that stage's role description with explicit evaluation criteria. The refiner receives the diagnostic tagged with the `stage_id`, looks at the current description for that stage, and produces a revised description that adds missing criteria, clarifies ambiguous ones, or makes the calibration instructions more concrete.

This is the self-improvement loop reading a confidence signal and acting on it. The judge's low score is not a failure — it is the system communicating that it lacks guidance. The loop adds the guidance.

---

## When quorum scores are consistently low

A judge that returns `"confidence": 0.5` on every run is not broken. It is telling you something. There are three possible causes, and the right response depends on which one applies.

**The judge description lacks evaluation criteria.** The most common cause. The judge has been given a task but no rubric for how to assess it. Fix: add explicit criteria to the role description (see the calibration guidance example above). The self-improvement loop will propose this automatically when it detects the pattern — but you can also fix it directly in the YAML without waiting for a loop cycle.

**The upstream worker is not producing enough evidence.** The judge is confident in its uncertainty — the analyst's output is thin, missing key fields, or too vague for the judge to work from. Look at the `output_valid_rate` for the upstream worker stage in the same HQS breakdown. If the worker is also degraded, fix the worker first. A better-described worker gives the judge more material; quorum scores often recover without any change to the judge itself.

**The task is genuinely ambiguous.** Some inputs have no clear answer. A contract that falls exactly on the line between medium and high risk, a customer message that is simultaneously positive and negative — these are real cases. A persistent `0.55–0.65` quorum score on genuinely hard inputs is not a prompt engineering failure; it is an honest signal. The right response here is to route low-confidence judge outputs to a human review queue rather than trying to prompt-engineer certainty out of the model.

```yaml
  - id: route_by_confidence
    role:
      name: Router
      type: orchestrator
      description: |
        Risk assessment: {{ risk_judge.risk_level }}
        Judge confidence: {{ risk_judge.confidence }}

        If confidence < 0.70, route to human_review.
        Otherwise route to automated_processing.
        Return {"queue": "human_review|automated_processing", "reason": "..."}.
    depends_on: [risk_judge]
```

Low quorum scores are signal, not noise. Chasing them to zero by rewriting prompts until the model always says `0.9` defeats the purpose.

---

## Quorum score vs. consensus quorum

Two distinct mechanisms both fall under the umbrella of "quorum" in Armature, and they operate at different levels.

**Declared quorum** (this document) is a judge's self-reported confidence in its own output. The judge tells you. It is a single value on a single stage trace. The harness reads it; no aggregation is needed.

**Structural quorum** (`fan_in: consensus`) is the harness measuring agreement across N independent runs of the same stage. It is not self-reported — the harness runs the prompt N times and detects consensus from the spread of outputs. A five-run consensus where all five agree produces a high-confidence aggregate; a split produces a low-confidence one.

Both contribute to quality assessment, but they answer different questions:

| | Declared quorum | Structural quorum |
|--|--|--|
| Source | Judge's output field | Harness agreement measurement |
| Granularity | Per-judge-trace | Per-fan-out-stage |
| Requires | `type: judge` + confidence field | `fan_in: consensus` |
| Measures | Model's epistemic confidence | Run-to-run consistency |
| HQS component | `avg_quorum_score` | `avg_quorum_score` |

A judge stage with `fan_in: consensus` produces both: the harness aggregates N judge runs into one consensus output, and the resulting output includes the judge's declared confidence in that consensus. Both are recorded.

---

## What the trace records

Every judge stage execution appends a `TraceRecord` with `quorum_score` set:

```python
TraceRecord(
    run_id="abc-123",
    workflow_name="contract-review",
    stage_id="risk_judge",
    role_type="judge",
    model="claude-opus-4-7",
    latency_ms=2210.0,
    success=True,
    output_valid=True,
    quorum_score=0.92,      # extracted from {"confidence": 0.92, ...}
    escalation_count=0,
)
```

If the judge returns no recognized numeric field in `[0.0, 1.0]`, `quorum_score` is `None` and that trace contributes `0.5` to `avg_quorum_score` — the noise-floor default. A judge that never emits a confidence field is indistinguishable from a consistently uncertain judge from the HQS perspective.

---

*Quorum score is the judge's voice in the metrics system. It is how a model communicates uncertainty upward to the harness, the self-improvement loop, and ultimately to the workflow author. Read it as signal, not as score to be maximized.*
