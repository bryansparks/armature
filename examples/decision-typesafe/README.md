# TypeSafe Decision Adapter — Layer 1 Spike

An A/B benchmark of the [TypeSafe AI decision API](https://docs.typesafe.ai)
(Jev / System One) against a conventional LLM stage, run *inside Armature*
with zero engine changes: the decision API is called through a plain
`parse: json` script adapter, exactly like any other shell tool.

This is the measurement that decides whether Armature grows a native
`decision:` stage type (Layer 2).

## What runs

```
load_benchmark ──► decide (fan-out ×20, script adapter → TypeSafe API)
                └─► llm_judge (fan-out ×20, guided_json → qwen3.6-27b small tier)
                              └──► compare (accuracy / agreement / latency / cost)
```

Both systems answer the same three questions about the same 20
hand-labeled code-review statements (`benchmark.json`):

| Question | Type | Armature equivalent |
|---|---|---|
| `is_concrete` | noul (0–1 truth) | boolean judgment |
| `severity` | choice (low/medium/high) | enum classification |
| `actionability` | score (0–2 ordinal) | ordinal regression |

## How to run

```bash
# from the repo root (PATH prefix so adapter python3 = venv python with httpx)
set -a; source .env; set +a        # TYPESAFE_API_KEY + OPENROUTER_API_KEY
PATH="$PWD/.venv/bin:$PATH" .venv/bin/armature run examples/decision-typesafe/ab-demo.yml

# offline, no keys, no network:
.venv/bin/python examples/decision-typesafe/decide.py \
  --questions-file examples/decision-typesafe/questions.json --dry-run \
  --state "api.py:44 leaks the auth header on redirect"
```

Tests (all HTTP mocked with `httpx.MockTransport`):

```bash
.venv/bin/python -m pytest tests/examples/test_decide.py tests/examples/test_compare.py tests/examples/test_typesafe_ab_demo.py -q
```

## Measured results (2026-09-21, run e97b81dd432b, 20 states)

| | TypeSafe decision (Jev) | LLM judge (qwen3.6-27b) |
|---|---|---|
| is_concrete accuracy | **1.00** (20/20) | 1.00 |
| severity accuracy | 0.80 | 0.95 |
| actionability accuracy | 0.80 | 1.00 |
| overall | 0.87 | 0.98 |
| avg latency / call | **260 ms** | **20,591 ms** (79×) |
| input tokens (20 calls) | 12,160 | 57,841 (+13,938 output) |
| metered cost (20 calls) | **$0.0005** ($42/Btok input, output free) | model-price dependent (cents) |

Decision-vs-judge agreement: 0.88 overall.

Run-to-run variance is real at n=20: an earlier run scored decision 0.85 /
judge 1.00; one item flips a dimension by 0.05. Treat ±1–2 items as noise.

### Where the decision API misses

Every severity/actionability miss sits at a genuine boundary:

- `s03` actionability score 1.27 (probabilities 0.02/0.69/0.29) — called 1,
  labeled 2. Near-boundary with split mass.
- `s06` actionability score 0.55 (0.45/0.55/0.0) — dead on the 0/1 boundary.
- `s08` actionability score 0.35 (0.65/0.35) — split between 0 and 1.
- `s18` severity: called high at 0.96 confidence, labeled medium — a
  *confident* miss, but "occasional production crashes" plausibly reads
  either way. The LLM judge called this one medium (matching the label).
- `s19` severity: both systems called medium; the label says high.

Misses cluster exactly where the probability mass straddles the decision
boundary — calibrated uncertainty on ambiguous statements, not confusion.
`is_concrete` (a factual either/or) was perfect.

## Read for the Layer 2 go/no-go

- **Noul (factual binary) judgments: excellent and free-tier cheap.** Route
  them to a decision stage with confidence.
- **Graded/ordinal judgments: usable but not LLM-grade** at this question
  wording — 0.8 with boundary-clustered misses. Options if adopted: reserve
  `score`/`choice` for coarse cuts, or use the returned probabilities as a
  router (escalate to an LLM only when mass splits across levels).
- **Latency/cost: not close.** 260 ms vs ~20.6 s per call at ~1/50th the
  input tokens; output tokens are free. A workflow gating every finding on
  a decision call would keep interactive pace.
- **Vendor-shape note:** the live response nests noul truth under
  `answers.<id>.noul` (not `truth`), echoes the question `type` per answer,
  and returns score legends/probabilities as string-indexed dicts. Mocked
  tests now encode the captured live shape.

### Honest caveats

- Labels are hand-authored for this spike; several "misses" are
  label-arguable (s18, s19). The label-independent agreement number (0.88)
  is the more robust signal.
- n=20, three runs total, one question battery, English-only input.
- The LLM judge prompt mirrors the question wording, but qwen is a
  generation model answering freeform-under-schema, not the same task
  Jev runs — the comparison is about *which tool to route to*, not model
  vs model.

## Files

| File | Role |
|---|---|
| `decide.py` | Script adapter: one state + questions → TypeSafe API, retries 429/529, `parse: json` stdout, `--dry-run` offline mode |
| `questions.json` | The three-question battery (noul/choice/score) |
| `benchmark.json` | 20 hand-labeled finding-style statements |
| `load_benchmark.py` | Emits states for the fan-out `partition_source` |
| `compare.py` | Accuracy/agreement/latency/cost summary + per-state detail |
| `ab-demo.yml` | The workflow itself |
