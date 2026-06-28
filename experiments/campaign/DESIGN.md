# Campaign Runner — Long-Run Armature Quality Trials (Design)

**Date:** 2026-06-26
**Author:** Bryan Sparks (with Claude)
**Status:** Design (awaiting approval before plan)
**Scope:** A **decoupled, black-box** test harness that drives Armature through long campaigns
of repeated workflow runs under varying conditions, observes HQS movement and self-improvement
behavior, renders a shareable report, and reproduces from recordings at zero LLM cost. Lives
**outside** the `armature` package; imports nothing from Armature's internals.

---

## 0. What this is, and what it is not

This is an **observational study and hypothesis-verdict harness**, not a stress/load tester.
The user's intent, verbatim: *"a watching of the memory, traces, HQS, our evaluations on those
HQS, and the deeper workings of Armature. It will test our many hypotheses and if they prove
out as we expect or hope for."* The goal is to turn *"this seems like a good idea"* into
*"wow, this thing really does … at scale and in mock operation modes"* — and to do it in a way
any third party can re-run, with a **pretty report** per campaign.

**What it is:**
- An external harness at top-level `experiments/campaign/` (peer to `armature/`, **not** inside it).
- Drives Armature **through the public CLI only** (`armature run | loop | improve | validate |
  dashboard --format json | replay`), as subprocesses — exactly as a real operator would.
- Observes Armature **only through the files it already writes** (per-campaign `--traces` sqlite,
  `*.improve_log.jsonl`, `*.spec_history.jsonl`, `*.pending.yaml`, dashboard JSON, captured
  stderr) plus **raw `sqlite3` reads** of the trace DB.
- **Reproduces the four HQS formulas itself** from raw trace rows, so it can independently check
  what Armature reports vs. what the rows imply — exposing any formula drift.
- Renders a **self-contained `report.html`** and a machine-readable `campaign.jsonl` per run.
- Supports **mock operation mode** via **record/replay** — re-render the exact same report from a
  prior recording, at zero LLM cost, no provider, no Armature changes.

**What it is not (explicitly out of scope):**
- **Not** a modification to Armature. No new `armature campaign` CLI command, no engine/validator/
  model changes, no packaging changes. *(An earlier draft referenced `armature campaign`; that
  was a carryover from a rejected in-tree approach. The harness has its own entrypoint — see §3.)*
- **Not** a mock LLM provider. Mock mode is record/replay, not a fake model server.
- **Not** `import armature.*` internals. The only Armature contact is the subprocess CLI + the
  sqlite traces file + the sidecar files Armature already writes.

### Observe-first, instrument-second (governing principle)

Modifying Armature to test Armature **taints the test**. The harness treats Armature as a black
box and measures it as-is. If a hypothesis can't be settled because Armature doesn't expose enough
to settle it, the harness logs an **observability gap** to `gaps.jsonl` (what we wanted to know,
what we needed to see, which file/command would have answered it). Those gaps become the
**data-justified** backlog of minimal Armature enhancements — only *after* the data shows the gap
matters. We never patch Armature mid-campaign to make a number look better.

---

## 1. Architecture & coupling boundary

```
experiments/campaign/            ← peer to armature/, NOT inside it
├── README.md                     # "run it yourself" guide
├── requirements.txt              # PyYAML, pydantic (stats + SVG are vendored/stdlib)
├── run.py                        # CLI entrypoint: python experiments/campaign/run.py <plan>
├── campaign/
│   ├── __init__.py
│   ├── plan.py                   # CampaignPlan schema, load + validate YAML
│   ├── cli_driver.py             # subprocess wrappers for armature run/loop/improve/…
│   ├── trace_io.py               # raw sqlite3 reads + sidecar readers (improve_log/history/pending)
│   ├── hqs.py                    # reproduce the 4 HQS formulas from raw trace rows; diff vs Armature's
│   ├── fault.py                  # input-difficulty ramp + spec-corruption (operates on a working copy)
│   ├── runner.py                 # CampaignRunner: serial phase → run → record → improve → recover → verdict
│   ├── verdicts.py               # evaluate the 4 hypotheses against campaign.jsonl
│   ├── stats.py                  # spearman, bootstrap CI (small, dependency-free)
│   ├── svgplot.py                # inline-SVG line/scatter/bar chart helpers (no matplotlib)
│   └── report.py                 # render self-contained report.html
├── plans/
│   ├── quick.yml                 # 2-minute demo (echo-workflow adapter-only; no API key)
│   ├── hqdynamics.yml            # the headline hypothesis trial
│   └── cold_vs_warm.yml          # cold (fresh) vs warm (carry-forward) memory trial
├── corpora/
│   └── difficulty.csv            # ordered input set for the difficulty-ramp lever
└── tests/
    ├── test_plan.py  test_fault.py  test_hqs.py
    ├── test_verdicts.py  test_report.py
    └── fixtures/demo_recordings/   # bundled so --replay works out of the box
```

**Coupling boundary (hard rules):**

1. **No `import armature.*`.** The harness depends on the *public surface* only: the CLI exit
   codes, the stdout/stderr text, and the on-disk artifacts. It may `sqlite3`-read a `--traces`
   file the operator pointed Armature at, because that file is a documented artifact, not an
   internal object.
2. **Drive via subprocess.** `cli_driver.py` shells out to `armature run …`, parses JSON where
   Armature offers it (`dashboard --format json`), and captures stderr for trigger/hint text.
3. **Per-campaign isolation.** Each campaign gets its own `--traces` DB and its own working copy
   of the spec (so spec-corruption and `improve` edits stay sandboxed and never touch the
   user's real spec). Runs are **serial** (one Armature process at a time) — this dissolves the
   spec-on-disk race that would bedevil parallel drivers, and it matches how a human operates.
4. **Reproduce, don't trust.** `hqs.py` recomputes every HQS formula from raw rows and compares
   against Armature's own reports (improve_log `hqs_before`, dashboard `current_hqs`, stderr
   hint). Divergence ≥ epsilon is logged and surfaced in the report, not silently corrected.

---

## 2. The campaign plan + run lifecycle

### CampaignPlan (declarative YAML)

```yaml
name: hqdynamics-baseline
description: "Does HQS track input difficulty, and does self-improve fire + recover?"
workflow: plans/specs/dangerous_pretzel_concept.yml   # the spec under trial
traces_db: out/hqdynamics-baseline/traces.sqlite       # per-campaign; fresh per campaign
working_spec: out/hqdynamics-baseline/spec_work.yml    # mutated copy; never the source

budget:                                               # campaign-wide guardrails
  max_runs: 60
  max_wallclock_hours: 6.0
  max_llm_calls: 4000
  max_tokens: 8_000_000

tiers:                                                # optional: pin or vary model tiers between phases
  baseline: { small: qwen/qwen3-6-27b, large: moonshotai/kimi-k2 }

phases:
  - id: ramp_easy_to_hard
    lever: input_difficulty_ramp                      # fault.py walks corpora/difficulty.csv in order
    inputs: { topic_field: "{{ corpus_row.topic }}", difficulty: "{{ corpus_row.level }}" }
    repeats: 1                                         # one run per difficulty level
    self_improve:                                     # after this phase, drive the improve loop
      enabled: true
      command: improve                                 # armature improve --no-apply (review) or --apply
      apply_auto_fields: true                          # description/on_fail/model_tier/timeout_s/loop
      max_rounds: 3
      target_hqs: 0.75
    gathers: [hqs_trace, improve_log, spec_history, pending, dashboard_json, stderr]

  - id: inject_spec_corruption
    lever: spec_corruption                            # fault.py mutates working_spec (e.g. garble a prompt)
    inputs: { seed: "{{ phase_index }}" }
    repeats: 3
    self_improve: { enabled: true, command: improve, apply_auto_fields: true, max_rounds: 3, target_hqs: 0.75 }
    gathers: [hqs_trace, improve_log, spec_history, pending, dashboard_json, stderr]

verdicts:                                             # thresholds seeded from §4; tunable per campaign
  hqs_tracks_difficulty:        { spearman_le: -0.50, p_le: 0.05 }
  self_improve_fires_and_recovers: { fires_within_k_traces: 5, edits_correct_surface: true,
                                     recovers_above: 0.75, within_r_runs: 5 }
  hqs_formula_consistency:      { max_abs_delta_le: 0.02, across_formulas: 4 }
  memory_carry_forward_helps:   { warm_minus_cold_mean_ge: 0.05, bootstrap_ci_lower_ge: 0.0 }
```

### Serial run lifecycle (one phase at a time; one Armature process at a time)

```
for phase in plan.phases:
    for rep in range(phase.repeats):
        1. PREPARE  — fault.py applies the lever (pick next difficulty row / corrupt working_spec)
        2. DRIVE    — cli_driver runs `armature run working_spec --traces db --input …` (or `loop`)
        3. RECORD  — append a campaign.jsonl row: run_id, phase, lever, inputs, exit_code,
                     hqs_by_formula{ours}, hqs_by_formula{armature}, deltas, gathered refs
        4. IMPROVE  — if phase.self_improve.enabled: drive `armature improve … [--apply|--no-apply]`;
                     parse *.improve_log.jsonl, *.spec_history.jsonl, *.pending.yaml; record
        5. RECOVER-PROBE — run the spec again (post-edit) to see if HQS recovered above threshold
    6. DIGEST — per-phase summary appended; feed verdicts
```

`gathers` controls which artifacts each run pins (by path) so the report and `gaps.jsonl` can
cite them. Everything the harness reads is something Armature already wrote.

---

## 3. Entrypoint, output, reproducibility, mock mode

### Entrypoint (NOT `armature campaign`)

```bash
# Real campaign (spends LLM budget)
python experiments/campaign/run.py plans/hqdynamics.yml

# Mock operation mode — re-render the same report from a recording, zero LLM cost
python experiments/campaign/run.py plans/hqdynamics.yml --replay out/hqdynamics-baseline/recordings/
python experiments/campaign/run.py plans/hqdynamics.yml --record              # write a recording while running

# Demo (adapter-only, no API key, ~2 min) — bundled recording replays instantly
python experiments/campaign/run.py plans/quick.yml --replay tests/fixtures/demo_recordings/
```

`--record` snapshots, per run: the exact `armature` argv, the captured stdout/stderr, the
dashboard JSON, the gathered sidecars, and the raw trace rows — enough that `--replay` can
regenerate `campaign.jsonl` and `report.html` deterministically **without invoking Armature or
any LLM**. Replaying a recording must byte-match a prior real run's `campaign.jsonl` (a test
asserts this), which is what makes "rerun the whole thing" trustworthy.

### Outputs (all under `<plan>.out/`)

| Artifact | Purpose |
|---|---|
| `campaign.jsonl` | one row per run: run_id, phase, lever, inputs, all 4 HQS values (ours + Armature's), deltas, gathered refs, exit code |
| `gaps.jsonl` | observability-gap log: hypothesis we couldn't settle, what we needed, which file/command would have answered it, severity |
| `recordings/` | the record/replay payload (argv, stdout/stderr, sidecars, trace rows) |
| `report.html` | the shareable deliverable (below) |
| `spec_work.yml` | the sandboxed mutated spec (corruption + improve edits land here, never the source) |

### `report.html` — self-contained, shareable

Single file, no external assets (inline SVG, inline CSS). Sections:
1. **Campaign summary** — name, git SHA of Armature + harness, plan hash, totals (runs,
   phases, llm calls, tokens, wallclock, improve firings).
2. **HQS over runs** — one SVG line per HQS formula (ours) overlaid with Armature's reported
   values; phase boundaries and lever events marked.
3. **Formula-divergence matrix** — per-formula `|ours − armature|` over time; any row crossing
   `max_abs_delta_le` is flagged. This is the honesty check.
4. **Fire → recover narratives** — for each self-improve firing: the trigger (n_traces, rolling
   HQS, target), the proposed edits (auto-applied vs. `.pending.yaml`), a unified diff of the
   spec before/after, and the recovery probe's HQS. Plain-English verdict per firing.
5. **Verdict table** — the 4 hypotheses × {metric, observed, threshold, PASS/FAIL/INCONCLUSIVE}.
6. **Observability gaps** — the `gaps.jsonl` digest, so readers see exactly where "we couldn't
   tell" and why.
7. **"Reproduce this" footer** — the exact `python experiments/campaign/run.py … --replay …`
   command + the recording dir, so anyone can regenerate this report locally.

---

## 4. Hypotheses & verdicts (the point of the whole thing)

Four hypotheses, each a verifiable verdict against `campaign.jsonl`. Thresholds below are **seed
defaults**; every campaign overrides them in `verdicts:`. INCONCLUSIVE is a first-class result —
it means the data couldn't settle it and points at a `gaps.jsonl` entry, not a quiet failure.

### H1 — HQS tracks input difficulty
**Expectation:** as the difficulty-ramp lever increases, HQS trends down (negative correlation).
**Measure:** Spearman ρ between difficulty level and per-run HQS; permutation p-value (vendored,
dependency-free) for significance.
**Verdict:** PASS if `ρ ≤ spearman_le` (e.g. −0.50) **and** `p ≤ p_le` (e.g. 0.05).

### H2 — Self-improve fires and recovers
**Expectation:** when rolling HQS drops below the trigger, `armature improve` fires; its
auto-applied edits surface correctly in the spec; a recovery probe lifts HQS back above threshold
within a bounded number of runs.
**Measure:** (a) firing within `fires_within_k_traces` (e.g. 5) of the drop; (b) `edits_correct_surface`
— the auto-apply fields (description / on_fail / model_tier / timeout_s / loop) appear verbatim in
the post-edit `spec_work.yml`; (c) recovery ≥ `recovers_above` (e.g. 0.75) within `within_r_runs`
(e.g. 5). Review-only fields (stages_added/removed, output_schema, safety_rules) appearing in
`*.pending.yaml` are noted but not required for PASS (they're gated by design).
**Verdict:** PASS iff all three.

### H3 — HQS formula consistency
**Expectation:** the four HQS computations Armature emits agree with our independent
recomputation from raw rows, within tolerance.
**Measure:** `max |ours − armature|` across the four formulas over all runs; flag any formula
whose delta crosses `max_abs_delta_le` (e.g. 0.02). A persistent nonzero delta reveals real
formula drift (e.g. the dashboard trend's missing HFR term, the `/60000` vs `/5000` latency
denominator) — a finding, not a bug in the harness.
**Verdict:** PASS iff no formula crosses tolerance on > `across_formulas`·runs fraction.

### H4 — Memory + carry-forward helps
**Expectation:** warm runs (carry-forward / continuation) score higher than paired cold runs
(fresh memory) on the same input, with the gap statistically real.
**Measure:** paired cold vs warm; mean Δ and a bootstrap 95% CI (vendored).
**Verdict:** PASS iff `mean Δ ≥ warm_minus_cold_mean_ge` (e.g. 0.05) **and** `CI lower ≥ 0`.

### The four HQS formulas the harness reproduces (pinned here so the spec is self-contained)

| Formula | Source | Weights/terms |
|---|---|---|
| authoritative per-run | `TraceStore.compute_hqs` | 0.35·output_valid + 0.25·success + 0.20·avg_quorum(default 0.5) + 0.10·latency(max(0,1−ms/5000)) + 0.10·hfr(esc==0 rate) |
| rolling (improve trigger) | `SelfImproveRunner._compute_hqs` | same weights; last 200 traces; run_id "rolling" |
| dashboard trend | `report/loader.py` | 0.40·valid + 0.30·success + 0.20·quorum + 0.10·latency(ms/60000); **no HFR** |
| `hqs_feedback` simple | `hooks/lifecycle.py` | 0.40·valid + 0.30·success + 0.20·fixed(0.5) + 0.10·latency |

Trigger thresholds in play: `0.75` (`--auto-improve` / `hqs_feedback`); `0.90` (`armature improve`
default, min 3 traces); none on `armature loop`. The harness treats these as constants observed
from behavior, not assumed.

---

## 5. Testing the harness itself

The harness is testable **without** spending LLM budget, because every code path has a recorded
fixture:
- `test_hqs.py` — feed canned trace rows, assert each reproduced formula matches a hand-computed
  value; assert divergence detection fires on a deliberately shifted row.
- `test_fault.py` — difficulty-ramp walks the corpus in order; spec-corruption produces a parseable
  spec whose diff is exactly the intended field.
- `test_verdicts.py` — synthetic `campaign.jsonl` rows exercise each verdict's PASS/FAIL/INCONCLUSIVE
  boundary, including the bootstrap-CI and Spearman paths.
- `test_report.py` — assert `report.html` is single-file (no external refs), contains all 7
  sections, and the "Reproduce this" command round-trips.
- **Replay determinism test** — `--replay tests/fixtures/demo_recordings/` must byte-match the
  `campaign.jsonl` committed alongside the fixture. This is the guarantee that "rerun the whole
  thing" is real and not theater.

---

## 6. Dependencies & footprint

`requirements.txt`: **PyYAML, pydantic** only. Stats (Spearman, permutation p, bootstrap CI) and
charts (inline SVG) are **vendored / stdlib** to keep "run it yourself" frictionless and to avoid
scipy/matplotlib as hard deps. `sqlite3` is stdlib. No Armature dependency.

---

## 7. Open decisions to confirm at plan time

1. **Naming.** Provisionally **"campaign runner"** (`experiments/campaign/`, plan verb "campaign").
   Floated alternatives: "soak test", "deep test", "long run", "HQDynamics". User leaned away
   from "soak." Decide before the plan doc.
2. **Where the spec/harness is committed.** `docs/superpowers/` is gitignored in this repo, so this
   design doc and the SDD plan are local scratch (matching the loop-driver precedent). The
   **harness code** at `experiments/campaign/` is *not* gitignored and **should** be committed —
   it's a shareable feature, not scratch. Confirm we commit `experiments/campaign/` to the repo
   (and whether to also track this design doc by relocating it out of the ignored dir).
3. **`improve` apply mode in campaigns.** Default to `--no-apply` (review-only, writes
   `.pending.yaml`) so the corruption-recovery path is observable without the harness silently
   rewriting specs; the plan's `apply_auto_fields: true` flips on the safe auto-apply fields only.
   Confirm this conservative default.

---

## 8. Non-goals

- No Armature modifications until `gaps.jsonl` + verdicts justify a specific minimal change.
- No mock LLM provider; mock mode = record/replay only.
- No parallel Armature processes (serial by design — eliminates the on-disk spec race).
- No cross-campaign aggregation in v1 (one report per campaign; multi-campaign rollups deferred).
- No new `armature` CLI subcommand.