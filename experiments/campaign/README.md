# Campaign Runner

A decoupled, black-box harness that drives Armature through long campaigns of
repeated runs under varying conditions, observes HQS movement and self-improvement,
and renders a self-contained `report.html`. It imports nothing from `armature` —
it drives Armature only via the public CLI and reads only the files Armature writes.

## Quick start

```bash
# Zero-cost demo: replay the bundled recording (no API key, no LLM, no deps beyond PyYAML+pydantic)
pip install -r experiments/campaign/requirements.txt
python experiments/campaign/run.py experiments/campaign/plans/quick.yml \
  --replay experiments/campaign/tests/fixtures/demo_recording

# Real campaign (spends LLM budget; needs OPENROUTER_API_KEY)
python experiments/campaign/run.py experiments/campaign/plans/hqdynamics.yml --record
```

The report lands at `out/<plan-name>/report.html`. Open it in any browser.

## What it tests

Four hypotheses (see `experiments/campaign/DESIGN.md` §4 for verdict thresholds):

1. **HQS tracks input difficulty** — does HQS fall as inputs get harder?
2. **Self-improve fires and recovers** — when HQS drops, does `armature improve` fire, edit the spec, and recover HQS?
3. **HQS formula consistency** — do the four HQS formulas Armature emits agree with an independent recomputation?
4. **Memory + carry-forward helps** — do warm runs beat paired cold runs on the judge-coverage signal, with model-failed runs excluded so the result is about memory, not model noise?

Plus a **soak / endurance test** (~500 runs across seven workflows + an overlapping-writes
concurrency phase) with eight reliability verdicts: no crashes, trace-DB integrity, no
concurrency row loss, no HQS drift, no latency creep, distinct run_ids, budget obeyed, and
real agent-spawn scale. The two budget-sensitive verdicts (`no_row_loss_under_concurrency`,
`agent_spawn_count`) are budget-aware: a shortfall caused by the wallclock budget stopping the
run early is INCONCLUSIVE, not a failure.

A plan declares only the verdicts it means to run, so (for example) `hqdynamics.yml` reports
H1/H2/H3 and never emits a spurious H4. Every campaign also runs the always-on `provider_health`
verdict, which FAILs and aborts on repeated provider-auth/credit errors.

## Run these tests yourself

Every verdict is reproducible at **zero LLM cost** from a recording. See
**[`docs/RESULTS-AND-LIMITATIONS.md`](docs/RESULTS-AND-LIMITATIONS.md)** for the exact
zero-cost replay commands for all three validation campaigns (plus the bundled demo), our
honest assessment of the latest run, and the deficiencies still open.

## Reproduce any report

Every `report.html` ends with a `Reproduce this` footer — the exact command to
regenerate it from its recording at zero LLM cost. Replays are byte-deterministic
(a test asserts this), so "rerun the whole thing" is real, not theater.

## Layout

- `campaign_runner/` — the harness (schema, sandbox isolation, HQS reproduction, levers, stats, verdicts, record/replay, CLI driver, runner, report)
- `plans/` — example campaign plans
- `corpora/` — input sets for the difficulty-ramp lever
- `tests/` — pytest suite, including a bundled demo recording for zero-cost replay