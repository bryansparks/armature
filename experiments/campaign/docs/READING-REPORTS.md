# Reading the Campaign Test Reports

> How to read the unified `index.html` and an individual `report.html`, what each
> section means, how to interpret the verdicts, and what artifacts sit underneath
> the HTML if you want the raw data.

## 1. Two layers of output

Every run writes a self-contained `report.html` (one test) and the harness
auto-rebuilds a unified `index.html` (all tests) after each run/replay. Both are
single static HTML files with inline styling and inline SVG charts — no external
assets, no JavaScript, nothing to install. Open them in any browser.

```
<out-dir>/                          # default: experiments/campaign/out
  index.html                         # unified table of ALL reports
  <plan-name>/
    report.html                      # the report for one test
    meta.json                        # campaign metadata + verdict results
    campaign.jsonl                   # one JSON line per run (the structured rows)
    gaps.jsonl                        # observability gaps
    run_<hash>.json                   # per-armature-run outputs (stage results)
    spec_work*.yml                    # the tier-override working specs
    .armature/traces.db               # armature's SQLite trace DB (per-stage rows)
    recording/                        # only with --record; enables zero-cost replay
      runs.jsonl
```

Start at `index.html` to see everything; click a row to open that test's
`report.html`.

## 2. The unified index (`index.html`)

A one-page table of every report found under the out-dir (scanned recursively,
so nested replay dirs are included). One row per report:

| Column | Meaning |
|--------|---------|
| **Test** | The plan name; links to that test's `report.html`. |
| **Kind** | What family the test belongs to: *Hypothesis (HQS dynamics)*, *Soak / reliability*, *Replay (determinism check)*, or *Campaign*. |
| **What it tests** | The plan's `purpose:` — one line on what this run was designed to demonstrate. |
| **Runs** | How many runs the plan executed. |
| **Verdicts** | A tally, e.g. `3 PASS, 1 FAIL`. |
| **Run date** | When the report was generated. |
| **Overall** | **PASS** (every verdict passed), **FAIL** (≥1 verdict failed), **INCONCLUSIVE** (≥1 verdict could not be settled and none failed), or `—` (no verdicts). Color-coded: green / red / orange / grey. |

Read it top-down: the most recent reports are at the top. **Overall** is the
headline — but it collapses detail, so click through to the report for the
per-verdict breakdown. A **Replay** row with the same Overall as its parent
run is the determinism check passing: the verdicts reproduced at zero cost.

## 3. An individual report (`report.html`)

Each report is the same fixed set of sections, top to bottom.

### 3.1 Header — what this test is
Title, a one-line description, and **"What this test is"**: the plan's `purpose`,
the plain-English claim the run was designed to test. Below it: the generated
date, the workflow spec, and the git SHA the engine was built at (so a report is
pinned to a specific revision of Armature).

### 3.2 Model tiers
The `model_tiers` the run actually used (after any `tier_override`). Lets you
see which models produced the data — important because a verdict can be
undermined by a too-weak model, and this row shows you that.

### 3.3 Campaign summary
`Totals`: how many runs and phases executed. A quick sanity check that the run
wasn't truncated by a budget.

### 3.4 HQS over runs
A line chart of HQS across the runs, two series:
- **authoritative** — the per-run, apples-to-apples HQS (recomputed by the
  harness from this run's own trace rows).
- **dashboard** — Armature's DB-wide rolling HQS (across all runs of this
  workflow).

For a difficulty-ramp campaign, you want to see **authoritative** fall as runs
get harder (H1). For a soak, you want both roughly flat (no drift).

### 3.5 Formula-divergence matrix
A bar showing the **max abs delta** between Armature's HQS and the harness's
independent recomputation, across the comparable channels. Tiny bar = the
formulas agree (H3). The authoritative channel is the apples-to-apples one;
rolling/dashboard compare per-run rows against Armature's across-run DB values
(scope-mismatched by design), so H3 judges authoritative only and reports the
others as informational `excluded`.

### 3.6 Fire → recover narratives
One paragraph per run where `armature improve` fired: the HQS it fired at, the
target, whether it applied an edit, and the recovery probe's HQS. This is the
human-readable story of H2 — you can read each firing and see whether HQS came
back. "(no firings recorded)" means H2 could not be exercised (→ INCONCLUSIVE).

### 3.7 Soak metrics
Only on soak reports: total agent spawns and the minimum expected. Confirms
the run was real scale, not a few calls.

### 3.8 Verdict table
The core. One row per verdict: **Hypothesis** (name), **Result** (PASS/FAIL/
INCONCLUSIVE, color-coded), **Detail** (the numbers the verdict was decided on).
This is where you go to see *why* Overall is what it is.

### 3.9 Observability gaps
What the harness wanted to measure but couldn't (e.g. "budget: stop before
max_runs" if a budget cut a phase short). Gaps don't fail the run — they explain
INCONCLUSIVE verdicts and warn about coverage holes.

### 3.10 Narrative
An overall sentence (PASS = supports the purpose; FAIL = does not; INCONCLUSIVE
= could not settle) followed by a bullet per verdict with its detail. The
plain-English summary you can quote.

### 3.11 Reproduce this
The exact command to regenerate *this report* from its recording at zero LLM
cost:
```bash
python experiments/campaign/run.py <plan> --replay <recording-dir>
```
This is the verification contract: anyone can reproduce your verdicts without
your API key or budget.

## 4. Verdict status semantics

| Status | Color | Meaning |
|--------|-------|---------|
| **PASS** | green | The run exercised the signal and the claim held. |
| **FAIL** | red | The run exercised the signal and the claim did **not** hold — a finding. |
| **INCONCLUSIVE** | orange | The run did not exercise the signal (too few data points, no firings, no hqs values). An observability note, **not** a quiet pass. |

Treat INCONCLUSIVE as "we don't know yet," not "ok." It usually means the run
was too small — e.g. `hqs_stability_no_drift` needs ≥8 runs with hqs values; a
smoke-scale soak returns INCONCLUSIVE for it, and the full soak resolves it.

## 5. Underneath the HTML: the JSON artifacts

The HTML is a *render* of the JSON — the JSON is the source of truth.

- **`campaign.jsonl`** — one JSON object per run (and one per concurrency-summary
  worker). The structured per-run record: `run_id`, `phase_id`, `lever`,
  `inputs`, `hqs_ours` (all four channels), `hqs_armature`, `improve_log`,
  `recovery_hqs_ours`, `exit_code`. This is what you load to re-plot or
  re-analyze anything in the report.
- **`meta.json`** — campaign name, purpose, date, git SHA, totals, and each
  verdict's `name` + `result` (the detail lives in the report's verdict table,
  recomputable from `campaign.jsonl` + `traces.db`).
- **`gaps.jsonl`** — the observability gaps, one per line.
- **`.armature/traces.db`** — Armature's own SQLite trace DB. One row per stage
  execution (`run_id`, `stage_id`, `role_type`, `model`, tokens, `latency_ms`,
  `success`, `quorum_score`, …). The reliability verdicts
  (`trace_db_integrity`, `agent_spawn_count`, `wallclock_stability`) query this
  directly. Inspect it with `sqlite3 out/<plan>/.armature/traces.db`.
- **`run_<hash>.json`** — the stage outputs Armature wrote for one run
  (via `armature run --output`).
- **`recording/runs.jsonl`** (only with `--record`) — the full per-run capture
  (argv, stdout, stderr, exit, trace_rows, hqs_armature). Appended one line per
  run, so it is durable incrementally — even a crashed long run leaves a
  complete recording of everything that completed, and `--replay` rebuilds the
  whole report from it at zero cost.

> **Durability note for long runs:** `campaign.jsonl`, `meta.json`, and
> `report.html` are written once, at the *end* of the run. If a multi-hour run
> is killed mid-way, the per-run raw data survives in `traces.db` +
> `run_*.json` (+ `recording/` if `--record`), and `--replay` regenerates the
> full report from the recording. This is why long soaks should always use
> `--record`.

## 6. How to verify any report yourself

1. Open `index.html`, find the row, note its Overall.
2. Open the report, read the Verdict table for the per-verdict detail.
3. Run the **Reproduce this** command from the report footer. It re-derives
   `campaign.jsonl` + verdicts + `report.html` from the recording with no LLM
   calls. The reproduced verdicts should match. (Replays are byte-deterministic;
   a test asserts this.)
4. If you want the raw numbers, load `campaign.jsonl` (JSONL — one JSON object
   per line) and/or query `traces.db` with `sqlite3`.

That is the whole loop: a claim, a run, a verdict, and a zero-cost way for
anyone to check it.