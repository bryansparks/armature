# The Armature Campaign Test Harness — What We Built and Why

> A black-box test harness that proves the Armature workflow engine does what it
> claims: that its quality score tracks reality, that its self-improvement loop
> actually fires and recovers, that its formulas are faithful, and that the engine
> stays up and clean under long, repeated, concurrent load.

## 1. The problem

Armature is a DAG-based multi-agent workflow engine: you describe an agentic
team in YAML, it runs the stages in dependency order, calls LLMs, and emits a
Hierarchical Quality Score (HQS) for each run. It also has a self-improvement
loop (`armature improve`) that edits a spec when HQS drops, and a replay system
that recomputes a run deterministically from its recorded trace.

Each of those is a *claim*. None of them is obviously true:

- Does HQS actually fall when inputs get harder, or is it noise?
- When HQS drops, does `armature improve` actually fire, edit the spec, and
  *recover* HQS — or does it fire and flail, or never fire at all?
- Do the four HQS numbers Armature emits agree with an independent
  recomputation from the raw trace rows, or has a formula drifted?
- Does the engine survive 500 repeated runs and overlapping concurrent writes
  against one shared trace DB, or does it corrupt, lose rows, or drift over time?

We could read the code and *believe* it. Instead we built a harness that
*runs the engine* and measures these things, then turns each measurement into a
named, pass/fail/inconclusive **verdict**. A green report is evidence; a red
report is a finding. Either is more useful than a code review.

## 2. The design discipline: black-box, replayable

Two rules shape everything here, and they are the reason the results are
trustworthy:

**Black-box.** The harness never `import armature.*`. It drives the engine only
through the public `armature` CLI as a subprocess, and observes only the files
Armature writes (`run_<hash>.json`, the SQLite trace DB, stdout). This means the
harness tests the *shipped artifact* — the same binary a user runs — not an
in-tree API that could quietly diverge from it. A bug that only shows when you
go through the CLI (argument parsing, strict validation, exit codes) is caught
here, not hidden.

**Replayable.** Every real run can be `--record`ed. A recording captures each
run's trace rows and outputs. `python run.py <plan> --replay <recording>` then
reconstructs the entire campaign — `campaign.jsonl`, the verdicts, and
`report.html` — at **zero LLM cost**, because the verdicts are computed from
recorded trace data, not from live model calls. Replays are byte-deterministic
(a test asserts this). So "reproduce this report" is a real command, not
theater, and an outsider can verify any verdict without spending money.

## 3. The two test families

The harness runs two kinds of test, each with its own verdicts:

### 3a. Hypothesis campaigns (H1–H4)

A *campaign* runs one workflow repeatedly while a **lever** varies something
about it — input difficulty, injected spec corruption, memory mode. Each lever
is designed to *move HQS in a known direction* so the harness can check whether
the engine's behavior tracks that movement. Four hypotheses:

| ID | Hypothesis | Lever | What "PASS" demonstrates |
|----|------------|-------|--------------------------|
| H1 | **HQS tracks input difficulty** | `input_difficulty_ramp` (easy → hard) | HQS monotonically falls as inputs get harder (negative Spearman correlation, p ≤ threshold). HQS is not noise — it responds to the task. |
| H2 | **Self-improve fires and recovers** | `model_tier_degradation` (break the judge's model tier so its LLM call errors → a failure trace → HQS drops) | When HQS drops below target, `armature improve` fires, edits the spec, and a recovery probe shows HQS returning above target. The improvement loop is closed and effective. (`spec_corruption` also exists as a lever, but did not reliably drop HQS on the research-brief workflow — `model_tier_degradation` does, deterministically, so the repaired plans use it for H2.) |
| H3 | **HQS formula consistency** | (cross-cutting) | The four HQS channels Armature emits (authoritative, rolling, dashboard, feedback) agree with an independent recomputation from raw trace rows. No formula has drifted. |
| H4 | **Memory + carry-forward helps** | cold vs warm runs (`memory_cold_warm`) | Warm runs beat their paired cold runs on the judge-coverage signal (`quorum_ours` — mean judge quorum, not aggregate HQS, whose latency term masked the memory effect), with the gap statistically real (bootstrap CI). Model-failed runs (a self-contradictory judge, or an empty researcher briefing) are excluded so the verdict is about memory, not model noise. |

Each verdict returns **PASS**, **FAIL**, or **INCONCLUSIVE**. INCONCLUSIVE is
not a quiet pass — it means the run did not exercise the signal (too few data
points, no firings, no hqs values). It is an *observability* result: the test
could not settle the claim, which is itself useful to know.

The example plans: `plans/h1-five-level.yml` (H1–H3 across five difficulty
levels), `plans/hqdynamics.yml` (H1–H3 with a difficulty ramp + model-tier
degradation — declares only H1/H2/H3; H4 is **not emitted** for it), `plans/cold_vs_warm.yml` (H4, the only plan that exercises the memory lever).

### 3b. The soak / endurance test

A *soak* is a campaign whose purpose is **reliability under load**, not output
correctness. It runs seven real workflows ~500 times on the cheap `tiny` tier
(tens of thousands of agent spawns) plus one overlapping-firings concurrency
phase (multiple `armature` processes writing to one shared trace DB at once),
then checks the engine stayed up and clean. Eight reliability verdicts:

| Verdict | What "PASS" demonstrates |
|---------|--------------------------|
| `no_unclean_exits` | Every run exited 0. No crashes over hundreds of iterations. |
| `trace_db_integrity` | `PRAGMA integrity_check` is `ok`; no NULL run_ids. The trace DB is uncorrupted. |
| `no_row_loss_under_concurrency` | Overlapping concurrent writes lost no rows: a clean shortfall (fewer reps than planned but zero SQLITE_BUSY) is INCONCLUSIVE — a budget stop or a per-rep model failure is not row loss. FAILs only when SQLITE_BUSY actually dropped rows. |
| `hqs_stability_no_drift` | HQS does not drift across the run (mean of the first quartile ≈ mean of the last quartile). |
| `wallclock_stability` | Per-run latency has no upward trend (slope ≤ threshold). No latency creep. |
| `checkpoint_resume_correctness` | Every `--force` rerun produced a distinct run_id (no stale-checkpoint attribution). |
| `budget_obeyed` | The run respected its `max_runs` budget. |
| `agent_spawn_count` | The soak spawned the expected scale of agents (≥ `min_total`). Budget-aware: a shortfall where the run stopped on the wallclock budget *before* the run cap is INCONCLUSIVE, not a failure — a genuine full-cap under-spawn still FAILs. |

The soak plan is `plans/soak.yml` (full, ~500 runs) with a reduced
`plans/soak-smoke.yml` for minute-scale smoke checks. Memory-subsystem
correctness is explicitly out of scope for the soak — it is a reliability test,
not a correctness test of any workflow's output.

## 4. Why this shape, and what a green run means

The shape is: **lever → expected movement → verdict**. We never just "run the
engine and see what happens." We set up a condition with a known expected
outcome, run, and check. That is what turns a raw run into a *claim about engine
behavior*.

A fully green campaign report is evidence that, for the workflows and
conditions exercised, the engine's HQS tracks difficulty, its self-improvement
loop closes, its formulas are faithful, and (for the soak) it survived hundreds
of runs and concurrent writes without corruption, drift, or loss. A red
verdict is a finding — and because every report is replayable at zero cost, a
finding can be handed to someone else to verify without spending their budget.

What a green run does *not* prove: correctness of any workflow's actual output
(the soak is deliberately not that), behavior on models/tiers not exercised,
or anything about workflows not in the plan. The claims are scoped to what the
plan exercised — and the report says exactly what that was.

## 5. Soak-test-finding refinements (#117–120)

The first full soak + replay pass surfaced four follow-on findings, all fixed on
`feat/soak-fixes-113-116` and pushed to PR #5. They are part of what the harness
checks now:

- **#117 — plan-declared verdicts only.** `all_verdicts` runs only the verdicts a
  plan declares (`None` = undeclared), plus the always-on `provider_health`. Stops
  spurious INCONCLUSIVE on undeclared verdicts (e.g. hqdynamics emitting an H4 it
  never claimed) dragging an otherwise-PASS report to INCONCLUSIVE.
- **#118 — H4 excludes model-failed runs.** A replay-deterministic per-run
  `model_failed` flag (shared `hqs.is_model_failed`: a self-contradictory judge
  — accept true *and* confidence < 0.5 — or an empty researcher briefing < 40 chars)
  is excluded from the H4 cold/warm comparison, and the verdict records
  `n_excluded_cold` / `n_excluded_warm`. The negative becomes about memory, not
  model noise.
- **#119 — `agent_spawn_count` is budget-aware.** A shortfall with the run stopped
  on the wallclock budget *before* the run cap is INCONCLUSIVE; only a full-cap
  under-spawn FAILs.
- **#120 — replay reconstructs `memory_mode`.** The recording now forward-captures
  per-run `memory_mode` (cold/warm); old recordings derive it by rep parity via a
  single shared `fault._fresh_for_memory` convention (live + replay cannot drift).
  This makes the H4 cold/warm split reproducible from a recording at zero cost,
  where before only a live run could produce it.

For our honest assessment of the latest run — including the deficiencies these
refinements do *not* yet close (the OpenRouter-credit block on #118's end-to-end
live demo; the H4 judge ceiling with no headroom; the synth-fanout-mid planner
guided_json failures) — see
**[`docs/RESULTS-AND-LIMITATIONS.md`](RESULTS-AND-LIMITATIONS.md)**, which also
gives the zero-cost replay commands to re-derive every verdict yourself.

## 6. Where to look next

- **`docs/RESULTS-AND-LIMITATIONS.md`** — our honest assessment of the latest run:
  what each campaign verdicted, what passed, what FAILED/INCONCLUSIVE, and the
  open deficiencies. Start here if you want the *results*, not the design.
- **`docs/READING-REPORTS.md`** — how to read the `report.html` and the unified
  `index.html`: every section, every verdict status, and the JSON artifacts
  underneath.
- **`README.md`** — quick start, including the zero-cost replay demo.
- **`DESIGN.md`** — the verdict thresholds and the lever definitions.
- **`docs/soak-test-design.md`** / **`docs/soak-test-plan.md`** — the
  implementation design and task-by-task plan for the soak subsystem.