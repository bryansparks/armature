# Soak Test Implementation Design

> **For agentic workers:** This is a design spec, not a task plan. After user review, `superpowers:writing-plans` turns it into a task-by-task implementation plan.

**Goal:** A black-box soak/endurance harness that runs 6–10 Armature workflows (aggregate ≥100 agents/iteration) ~500 times on the cheap `tiny` tier — spawning tens of thousands of agents — and produces a shareable `report.html` proving the engine stays up, clean, and stable under cron-style repetition, including overlapping firings.

**Architecture:** Extend the existing Campaign Runner (black-box: no `import armature.*`; drive via `armature` CLI subprocess; observe via files + raw `sqlite3`). A soak is a `CampaignPlan` whose phases span multiple workflows and include one concurrency phase. New: per-phase `workflow:` override, `concurrency:` phase option, `tier_override`, per-phase working specs, a shared-sandbox concurrency mode, and a `soak_verdicts` module. One plan → one `report.html`, fully replayable at zero LLM cost.

**Tech stack:** Python 3.11, PyYAML, pydantic, stdlib `subprocess`/`sqlite3` (existing harness stack). OpenRouter via `OPENROUTER_API_KEY` (gitignored, chmod 600).

## Primary goal & scope

**Primary (locked):** reliability/longevity — prove the engine performs over many iterations under cron. Memory-subsystem correctness is explicitly **out of scope** (separate test elsewhere).

**In scope:**
- Multi-workflow soak (~500 real tiny-tier runs, ≥100 agents/iteration, ~50k+ agent spawns).
- One overlapping-firings concurrency phase (2–3 workers against one shared trace DB).
- Reliability verdicts: no unclean exits, trace-DB integrity, no row-loss under concurrency, HQS drift, wallclock stability, checkpoint-resume correctness, budget obedience, agent-spawn count.
- Replay reproduces all soak verdicts at zero cost.

**Out of scope:** memory write/read/isolation assertions; H1–H4 hypothesis verdicts (unchanged); replay-multiply as a primary scale lever (replay doesn't spawn real agents).

## Global constraints

- **Black-box only:** never `import armature.*`. Drive via `armature` CLI subprocess; observe via files + raw `sqlite3`. Existing harness rule.
- **Branch hygiene:** work on `feat/soak-test` (branched from `feat/campaign-runner`), no direct-to-main, no push unless requested. Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- **Secret hygiene:** `OPENROUTER_API_KEY` lives in `experiments/campaign/.env` (gitignored, chmod 600). Never echo/commit it.
- **No regressions:** the existing 63 campaign tests must stay green. H1–H4 verdicts and the single-workflow plan path are unchanged (additive only).
- **Tier pinning:** every workflow's `model_tiers` is rewritten to the soak `tiny` tier (`qwen/qwen3.6-27b` via OpenRouter — proven in campaign runs #1–#3). No ollama/anthropic/local providers in the soak.
- **Replay determinism:** soak verdicts must be computable from the recording alone (no re-invocation of Armature). Report timestamp stays out of `campaign.jsonl`.

## File structure

**Modify (existing):**
- `experiments/campaign/campaign_runner/plan.py` — add `Phase.workflow`, `Phase.concurrency`, `CampaignPlan.tier_override`, `CampaignPlan.soak_verdicts`.
- `experiments/campaign/campaign_runner/sandbox.py` — per-phase working-spec paths; `shared` sandbox mode for concurrency.
- `experiments/campaign/campaign_runner/runner.py` — per-phase workflow resolution + tier-override application + concurrency phase execution; route soak verdicts.
- `experiments/campaign/campaign_runner/report.py` — soak report section (curves, spawn total, DB growth).
- `experiments/campaign/campaign_runner/verdicts.py` — `all_verdicts` also invokes soak verdicts when `plan.soak_verdicts` is set (additive).

**Create (new):**
- `experiments/campaign/campaign_runner/soak_verdicts.py` — reliability verdict functions + `all_soak_verdicts(rows, plan)`.
- `experiments/campaign/campaign_runner/concurrency.py` — the concurrent-worker fan-out against a shared sandbox DB.
- `experiments/campaign/specs/soak/` — workflow specs:
  - `real_research_pipeline.yml`, `real_deliberation.yml`, `real_competitive_analysis.yml`, `real_iterative_refinement.yml` (copies of the repo examples, tiers overridden at runtime).
  - `synth_fanout_wide.yml` (planner → fan_out 40 workers → synthesizer), `synth_fanout_deep.yml` (planner_a → fan_out 15 → planner_b → fan_out 15 → synthesizer; depth-5 DAG, 30 agents), `synth_fanout_mid.yml` (planner → fan_out 30 workers → synthesizer).
- `experiments/campaign/plans/soak.yml` — the soak campaign plan.
- `experiments/campaign/tests/test_soak.py` — schema, tier-override, per-phase isolation, concurrency integrity, verdict logic.

**Reuse unchanged:** `cli_driver.py` (already `--force`), `fault.py`, `record.py`, `trace_io.py`, `hqs.py`.

## Component designs

### plan.py — schema additions

```python
class Concurrency(BaseModel):
    workers: int = 2              # parallel subprocesses against the shared DB
    driver: str = "armature_loop" # "armature_loop" | "armature_run_force"
    shared_db: bool = True        # all workers HOME -> one sandbox dir
    reps_per_worker: int = 20

class Phase(BaseModel):
    # ...existing fields...
    workflow: str | None = None   # overrides plan.workflow for this phase
    concurrency: Concurrency | None = None

class TierOverride(BaseModel):
    apply: bool = True
    tiers: dict[str, dict] = Field(default_factory=dict)  # e.g. {"tiny": {provider,model,api_key_env,...}}

class SoakVerdicts(BaseModel):
    no_unclean_exits: dict = Field(default_factory=dict)
    trace_db_integrity: dict = Field(default_factory=dict)
    no_row_loss_under_concurrency: dict = Field(default_factory=dict)
    hqs_stability_no_drift: dict = Field(default_factory=dict)
    wallclock_stability: dict = Field(default_factory=dict)
    checkpoint_resume_correctness: dict = Field(default_factory=dict)
    budget_obeyed: dict = Field(default_factory=dict)
    agent_spawn_count: dict = Field(default_factory=dict)

class CampaignPlan(BaseModel):
    # ...existing fields...
    purpose: str = ""               # top-of-report "what this test is" paragraph (falls back to description)
    tier_override: TierOverride | None = None
    soak_verdicts: SoakVerdicts | None = None
```

Validators: `Concurrency.driver` in `{"armature_loop","armature_run_force"}`; `Phase.workflow` resolved relative to the plan file's directory; `concurrency` and `self_improve` are mutually exclusive on a phase (validator error `CONCURRENCY_AND_SELF_IMPROVE_CONFLICT`).

### sandbox.py — per-phase working specs + shared mode

- Replace the single `working_spec` with a per-phase path: `working_spec_for(phase_id) -> Path` returning `self.dir / f"spec_work_{phase_id}.yml"`. The existing `spec_work.yml` is kept as the default for phases without a `workflow` override (back-compat).
- `apply_tier_override(spec_path: Path, override: TierOverride) -> None`: load YAML, replace every entry in `model_tiers` with the override's `tiny` tier (preserving tier **names** so stage `model_tier` references still resolve), write back. Idempotent.
- `shared_root` for concurrency: a `Sandbox` constructed with a `shared=True` flag points `dir` at an existing sandbox dir (so multiple workers write the same `.armature/traces.db`). The parent creates it; workers attach.

### runner.py — per-phase workflow + concurrency

- At phase start: resolve the phase's workflow (phase.workflow or plan.workflow), copy it to `working_spec_for(phase.id)`, apply `tier_override` if set, validate.
- `workflow_name` becomes per-phase (parsed from that phase's spec `name`) so trace rows are correctly attributed.
- A phase with `concurrency` is dispatched to `concurrency.run_workers(...)` instead of the serial rep loop; its result rows (one summary row per worker + the integrity assertions) are appended.
- `lever` for soak phases is `none` (no fault injection); `apply_lever` still runs (no-op for `none`) so the existing path is reused.
- `_finalize` routes to `soak_verdicts.all_soak_verdicts` when `plan.soak_verdicts` is set, in addition to the existing H1–H4 `all_verdicts` (which no-op when their `verdicts:` block is empty, as in the soak plan).

### concurrency.py — overlapping-firings stress

```python
def run_workers(sb: Sandbox, spec: Path, conc: Concurrency, phase_id: str,
                recording: Recording | None) -> list[dict]:
    # Spawn `workers` subprocesses in parallel, each with env HOME=<sb.dir>
    # (shared DB). Each worker runs `armature loop <spec> --max-runs reps_per_worker`
    # (driver=armature_loop) OR `armature run <spec> --force` reps_per_worker times
    # (driver=armature_run_force). Wait for all. Record each worker's argv/stderr/exit.
    # Returns one summary row per worker with: run_ids, exit_codes, n_trace_rows,
    # sqlite_busy_count (stderr scan for "database is locked"/"SQLITE_BUSY").
```

Assertions live in the verdict, not here — this module only executes and observes. Black-box: only subprocess + the shared `traces.db` + stderr.

### soak_verdicts.py — reliability signals

Each function returns `(name, status, detail)` where status ∈ {PASS, FAIL, INCONCLUSIVE}. Rows carry the existing schema plus soak summary rows from the concurrency phase.

| Verdict | PASS criterion (default threshold) | Detail fields |
|---|---|---|
| `no_unclean_exits` | `n_unclean == allowed_failures` (0) | `n_runs`, `n_unclean`, `bad_run_ids` |
| `trace_db_integrity` | DB opens; `PRAGMA integrity_check` ok; 0 rows with NULL `run_id` | `integrity_check`, `n_null_run_id`, `n_rows` |
| `no_row_loss_under_concurrency` | FAIL only on `sqlite_busy_count > 0` (definitive row loss); a clean shortfall (actual < expected, busy = 0) → INCONCLUSIVE (budget stop or per-rep model failure, not row loss); full reps + busy = 0 → PASS (#116) | `expected`, `actual`, `sqlite_busy_count`, `all_exit0`, `per_worker_rows` |
| `hqs_stability_no_drift` | abs(mean HQS first-quartile − last-quartile) ≤ 0.08 | `q1_mean`, `q4_mean`, `delta` |
| `wallclock_stability` | per-run latency OLS slope ≤ 5.0 ms/run | `slope_ms_per_run`, `n` |
| `checkpoint_resume_correctness` | every `--force` rerun has a distinct `run_id` (no dups) | `n_runs`, `n_distinct_run_ids`, `dup_run_ids` |
| `budget_obeyed` | campaign stopped via budget, not crash; `len(rows) <= max_runs` | `n_rows`, `max_runs`, `stop_reason` |
| `agent_spawn_count` | `total ≥ min_total` → PASS; `total < min_total AND n_main_runs < max_runs` → INCONCLUSIVE (wallclock-budget stop, not an under-spawn); `total < min_total AND n_main_runs ≥ max_runs` → FAIL (full cap, genuine under-spawn) (#119) | `total_agents`, `min_total`, `n_main_runs`, `max_runs` |

Agent-spawn count = sum over rows of trace rows for that `run_id` where `role_type` is an LLM stage (`worker`/`researcher`/`judge`/`orchestrator`), **excluding** `tool_call`/`adapter`/`gate` stages — i.e. real LLM agent calls, derived from the trace DB, not estimated.

**Concurrency summary rows** (one per worker, appended to `campaign.jsonl`): `{"run_id": null, "phase_id": <phase>, "lever": "none", "is_concurrency_summary": true, "worker": <i>, "run_ids": [...], "exit_codes": [...], "n_trace_rows": <int>, "sqlite_busy_count": <int>, "hqs_ours": <all_four over the worker's rows or null>, "hqs_armature": null}`. The `is_concurrency_summary` flag lets the report + replay distinguish them from main rows. Replay restores them verbatim from the recording's meta.

### report.py — soak section

When `plan.soak_verdicts` is set, append a soak section: HQS-over-run-index line chart, latency-over-run-index line chart, DB-size-growth curve, agent-spawn total, concurrency-phase worker summary, and the soak verdict table. Reuse the existing header (name, date, git_sha, tiers, totals) and reproduce-cmd.

### Report UX — purpose, narrative, index page (applies to ALL campaign reports)

Outsiders run these tests themselves, so every `report.html` must be self-explanatory and the suite must have a single entry point. Three additions, all render-time (no new data collection; derived from existing `campaign.jsonl` + verdicts + plan metadata):

1. **Top "What this test is" paragraph** — rendered at the top of every report, above the verdict table. Sourced from a new plan field `purpose:` (a short prose string; falls back to `description:` if absent). For the H1 campaign it states the HQS-tracks-difficulty / self-improve / formula-consistency hypotheses; for the soak it states the reliability/longevity goal. Written for a reader who has never seen Armature.

2. **Bottom narrative evaluation** — a generated prose section at the foot of every report summarizing the run as **good / bad / inconclusive** per verdict, in plain language. A pure function `narrative(verdicts, rows, plan) -> str` maps each verdict status (PASS/FAIL/INCONCLUSIVE) to a sentence, plus an overall one-line verdict ("This run supports / does not support / is inconclusive for the stated purpose"). No hand-authoring per run — derived from the data so replay reproduces an identical narrative. The soak narrative additionally summarizes agent-spawn total, DB growth, and any concurrency row-loss.

3. **Index page** — a new `build_index(out_dir) -> Path` in `report.py` (and a `python experiments/campaign/run.py --build-index <out_dir>` CLI flag) that scans `out/*/report.html`, reads each run's `campaign.jsonl` + `gaps.jsonl` for its header (name, date, git_sha, totals) and verdict statuses, and renders a single self-contained `out/index.html`. Each row: test name, one-line "what it tests" (from that run's `purpose`), when run (date), overall verdict (good/bad/inconclusive), and a link to its `report.html`. The index links to every campaign/soak report under `out/` — the shareable landing page. Run automatically at the end of every campaign/soak run, and independently via `--build-index`.

These are additive to `report.py`; existing report fields/structure unchanged. The narrative function is unit-tested on synthetic verdict fixtures (PASS/FAIL/INCONCLUSIVE each); the index builder is tested on a fake `out/` tree with two reports. The H1 campaign plan (`plans/h1-five-level.yml`) gains a `purpose:` field so its existing reports pick up the paragraph + narrative on the next run.

## Data flow

```
soak.yml plan
  └─ CampaignRunner.run()
       per phase:
         resolve workflow -> spec_work_<phase>.yml -> tier_override -> validate
         if concurrency: concurrency.run_workers (N parallel armature loop/run --force, shared HOME)
         else: serial reps (existing path, lever=none)
         record_run(meta={phase_id, lever, workflow, inputs, ...})
       └─ _finalize: write campaign.jsonl + gaps.jsonl
            verdicts.all_verdicts (H1-H4, no-op) + soak_verdicts.all_soak_verdicts
            render_report (with soak section) -> report.html
```

Replay (`--replay`): reconstructs `campaign.jsonl` from the recording (existing path, unchanged), then `_finalize` recomputes soak verdicts from the replayed rows + the recorded concurrency summary rows. Zero Armature/LLM cost.

## Error handling

- A workflow that fails `validate` after tier override → `gaps` entry severity high, phase skipped (existing pattern). Soak verdict `no_unclean_exits` will then FAIL — which is the correct signal.
- A concurrency worker that exits non-zero (e.g. `SQLITE_BUSY` crash) → recorded with its exit code + stderr; verdict `no_row_loss_under_concurrency` / `no_unclean_exits` adjudicate. We do **not** retry inside a worker — a BUSY crash is a real finding to surface, not mask.
- Budget trip mid-soak → `budget_obeyed` records `stop_reason="budget"`; partial rows are still verifiable.
- Tier override fails to parse a workflow → raise a clear plan-load error (fail fast, don't silently run an expensive tier).

## Testing

`tests/test_soak.py`:
- Schema: plan with per-phase `workflow`, `concurrency`, `tier_override`, `soak_verdicts` loads and validates; `CONCURRENCY_AND_SELF_IMPROVE_CONFLICT` rejected.
- `apply_tier_override`: every `model_tiers` entry rewritten to tiny, tier names preserved, idempotent, stage `model_tier` refs still resolve.
- Per-phase working-spec isolation: two phases with different workflows get distinct `spec_work_<phase>.yml` files and distinct `workflow_name`s.
- Concurrency integrity: 3 fake worker threads each `sqlite3.connect` the same DB and insert N rows (WAL); assert `actual == expected`, no NULL run_ids, integrity_check ok. (No real `armature` call.)
- Each soak verdict's PASS/FAIL on synthetic row fixtures (drift, latency-slope, dup run_ids, low spawn count).
- Replay of a soak recording reproduces soak verdicts (status match).

Existing `tests/test_runner.py`, `test_cli_driver.py` unchanged and green (single-workflow path back-compat: `Phase.workflow=None` falls back to `plan.workflow`).

## Scale, budget, cost

- ~500 real runs across 7 workflows (4 real + 3 synthetic). Per-iteration aggregate agents: synth (40+30+30 = 100) + real (~20) ≈ 120. × ~500 reps ≈ **60k agent spawns**.
- Concurrency phase: 3 workers × 20 reps = 60 runs against one DB.
- Budget: `max_runs: 600`, `max_llm_calls: 15000`, `max_wallclock_hours: 3.0`, `max_tokens: 4000000`.
- **Budget unit note:** the campaign runner's `max_llm_calls` counts `armature run` *invocations* (~1 per rep + improve/probe), **not** individual agent LLM calls. So ~560 run-invocations fit comfortably under 15000; the ~65k agent spawns are counted separately by the `agent_spawn_count` verdict from trace rows. Do not read `max_llm_calls` as an agent-call cap.
- Cost: a few USD on `qwen/qwen3.6-27b`.

## Reproducibility

`report.html` is self-contained. An outsider runs `python experiments/campaign/run.py plans/soak.yml --replay <recording>` and sees identical soak verdicts at zero cost. The reproduce-cmd is printed in the report header.