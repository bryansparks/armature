# Soak Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Campaign Runner into a black-box soak/endurance harness that runs 7 workflows (~120 agents/iteration) ~500 times on the cheap tiny tier (~60k agent spawns), plus an overlapping-firings concurrency phase, and renders a self-explanatory report + a single index page linking all reports.

**Architecture:** Additive extensions to the existing black-box chassis (no `import armature.*`): per-phase `workflow:` override, `concurrency:` phase, `tier_override`, a `soak_verdicts` module, a `concurrency` module, and report UX (purpose paragraph, bottom narrative, index page). One plan → one `report.html`, fully replayable at zero cost.

**Tech Stack:** Python 3.11, PyYAML, pydantic v2, stdlib `subprocess`/`sqlite3`/`threading`. OpenRouter via `OPENROUTER_API_KEY`.

## Global Constraints

- **Black-box only:** never `import armature.*`; drive via `armature` CLI subprocess; observe via files + raw `sqlite3`.
- **Branch:** work on `feat/soak-test` (already created from `feat/campaign-runner`); no direct-to-main, no push unless requested. Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.
- **Secret hygiene:** `OPENROUTER_API_KEY` in `experiments/campaign/.env` (gitignored, chmod 600); never echo/commit.
- **No regressions:** existing 63 tests stay green; H1–H4 verdicts + single-workflow plan path unchanged (additive only).
- **Tier pinning:** every workflow's `model_tiers` rewritten at runtime to `qwen/qwen3.6-27b` via OpenRouter (`OPENROUTER_API_KEY`).
- **Replay determinism:** soak verdicts computable from the recording alone; report timestamp stays out of `campaign.jsonl`.
- **Run from repo root** for `armature` CLI calls: `armature` must be on PATH (it is, installed editable). Tests use `pytest` from `experiments/campaign/`.

---

## File structure (locked)

**Modify:**
- `experiments/campaign/campaign_runner/plan.py` — schema additions (Task 1).
- `experiments/campaign/campaign_runner/sandbox.py` — `working_spec_for` + `apply_tier_override` + `copy_working_spec_to` (Task 2).
- `experiments/campaign/campaign_runner/runner.py` — per-phase workflow resolution + tier override (Task 3), `_finalize` soak routing + `meta.json` (Task 5), concurrency phase dispatch (Task 7).
- `experiments/campaign/campaign_runner/report.py` — purpose paragraph + `narrative` + soak metrics + `build_index` (Tasks 8, 9).
- `experiments/campaign/campaign_runner/cli.py` — `--build-index` flag (Task 9).
- `experiments/campaign/plans/h1-five-level.yml` — add `purpose:` (Task 11).

**Create:**
- `experiments/campaign/campaign_runner/soak_verdicts.py` (Task 4).
- `experiments/campaign/campaign_runner/concurrency.py` (Task 6).
- `experiments/campaign/specs/soak/real_research_pipeline.yml`, `real_deliberation.yml`, `real_competitive_analysis.yml`, `real_iterative_refinement.yml`, `synth_fanout_wide.yml`, `synth_fanout_deep.yml`, `synth_fanout_mid.yml` (Task 10).
- `experiments/campaign/plans/soak.yml` (Task 11).
- `experiments/campaign/tests/test_soak.py` (Tasks 1–9 each add their tests here).

---

### Task 1: plan.py — soak schema additions

**Files:**
- Modify: `experiments/campaign/campaign_runner/plan.py`
- Test: `experiments/campaign/tests/test_soak.py` (create)

**Interfaces:**
- Produces: `Concurrency(workers, driver, shared_db, reps_per_worker)`, `TierOverride(apply, tiers)`, `SoakVerdicts(...)` (8 dict fields), `Phase.workflow: str|None`, `Phase.concurrency: Concurrency|None`, `CampaignPlan.purpose: str`, `CampaignPlan.tier_override: TierOverride|None`, `CampaignPlan.soak_verdicts: SoakVerdicts|None`. Validator: a phase with both `concurrency` and an enabled `self_improve` raises `CONCURRENCY_AND_SELF_IMPROVE_CONFLICT`. `Concurrency.driver` must be in `{"armature_loop","armature_run_force"}`; `workers >= 2`.

- [ ] **Step 1: Write the failing tests** (`tests/test_soak.py`, create file)

```python
import textwrap
import pytest
from pathlib import Path
from campaign_runner.plan import load_plan


def _soak_plan_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        description: "x"
        workflow: specs/soak/synth_fanout_mid.yml
        purpose: "Soak test: prove the engine stays up under cron repetition."
        tier_override:
          apply: true
          tiers:
            tiny: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
        budget: {max_runs: 5}
        phases:
          - id: mid
            lever: none
            inputs: {topic: "q"}
            repeats: 2
          - id: overlap
            workflow: specs/soak/synth_fanout_wide.yml
            lever: none
            concurrency: {workers: 3, driver: armature_loop, reps_per_worker: 5}
        soak_verdicts: {no_unclean_exits: {allowed_failures: 0}, agent_spawn_count: {min_total: 100}}
        verdicts: {}
    """))
    return p


def test_soak_plan_loads(tmp_path):
    plan = load_plan(_soak_plan_yaml(tmp_path))
    assert plan.purpose.startswith("Soak test")
    assert plan.tier_override.apply is True
    assert plan.tier_override.tiers["tiny"]["model"] == "qwen/qwen3.6-27b"
    assert plan.soak_verdicts is not None
    assert plan.soak_verdicts.agent_spawn_count == {"min_total": 100}
    assert plan.phases[0].workflow is None
    assert plan.phases[1].workflow == "specs/soak/synth_fanout_wide.yml"
    assert plan.phases[1].concurrency.workers == 3
    assert plan.phases[1].concurrency.driver == "armature_loop"


def test_concurrency_and_self_improve_conflict_rejected(tmp_path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases:
          - id: x
            lever: none
            inputs: {topic: "q"}
            repeats: 1
            self_improve: {enabled: true, target_hqs: 0.75, min_traces: 1, max_rounds: 1, apply: false}
            concurrency: {workers: 2, driver: armature_loop}
        verdicts: {}
    """))
    with pytest.raises(Exception):
        load_plan(p)


def test_concurrency_bad_driver_rejected(tmp_path):
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases:
          - id: x
            lever: none
            inputs: {topic: "q"}
            repeats: 1
            concurrency: {workers: 2, driver: bogus}
        verdicts: {}
    """))
    with pytest.raises(Exception):
        load_plan(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: FAIL — `AttributeError`/`ValidationError` (fields don't exist yet).

- [ ] **Step 3: Implement the schema** (add to `plan.py`)

Add `model_validator` to the pydantic import line at top:
```python
from pydantic import BaseModel, Field, field_validator, model_validator
```

Add these models above `Phase`:
```python
class Concurrency(BaseModel):
    workers: int = 2
    driver: str = "armature_loop"
    shared_db: bool = True
    reps_per_worker: int = 20

    @field_validator("driver")
    @classmethod
    def _check_driver(cls, v: str) -> str:
        if v not in {"armature_loop", "armature_run_force"}:
            raise ValueError(f"unknown concurrency driver: {v!r} (known: armature_loop, armature_run_force)")
        return v

    @field_validator("workers")
    @classmethod
    def _check_workers(cls, v: int) -> int:
        if v < 2:
            raise ValueError("concurrency.workers must be >= 2")
        return v


class TierOverride(BaseModel):
    apply: bool = True
    tiers: dict[str, dict] = Field(default_factory=dict)


class SoakVerdicts(BaseModel):
    no_unclean_exits: dict = Field(default_factory=dict)
    trace_db_integrity: dict = Field(default_factory=dict)
    no_row_loss_under_concurrency: dict = Field(default_factory=dict)
    hqs_stability_no_drift: dict = Field(default_factory=dict)
    wallclock_stability: dict = Field(default_factory=dict)
    checkpoint_resume_correctness: dict = Field(default_factory=dict)
    budget_obeyed: dict = Field(default_factory=dict)
    agent_spawn_count: dict = Field(default_factory=dict)
```

Add fields + validator to `Phase` (after `fresh_db: bool = False`):
```python
    workflow: str | None = None
    concurrency: Concurrency | None = None

    @model_validator(mode="after")
    def _check_concurrency_self_improve(self):
        if (self.concurrency is not None and self.self_improve is not None
                and self.self_improve.enabled):
            raise ValueError(
                "CONCURRENCY_AND_SELF_IMPROVE_CONFLICT: a phase may not set both "
                "concurrency and an enabled self_improve")
        return self
```

Add fields to `CampaignPlan` (after `verdicts`):
```python
    purpose: str = ""
    tier_override: TierOverride | None = None
    soak_verdicts: SoakVerdicts | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `cd experiments/campaign && python -m pytest -q`
Expected: PASS (66 passed: 63 existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add experiments/campaign/campaign_runner/plan.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): soak schema — per-phase workflow, concurrency, tier_override, soak_verdicts

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: sandbox.py — per-phase working spec + tier override

**Files:**
- Modify: `experiments/campaign/campaign_runner/sandbox.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `Sandbox.working_spec_for(phase_id: str) -> Path` (`<dir>/spec_work_<phase_id>.yml`); `Sandbox.copy_working_spec_to(source: Path, target: Path) -> Path`; `Sandbox.apply_tier_override(spec_path: Path, override: TierOverride | None) -> None` — rewrites every `model_tiers` entry to the override's `tiny` tier, preserving tier **names** so stage `model_tier` references still resolve. Idempotent. No-op when `override is None` or `not override.apply` or no `tiny` tier or the spec has no `model_tiers`.
- Consumes: `TierOverride` from Task 1.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_working_spec_for_distinct_paths(tmp_path):
    from campaign_runner.sandbox import Sandbox
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 1}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    assert sb.working_spec_for("a").name == "spec_work_a.yml"
    assert sb.working_spec_for("a") != sb.working_spec_for("b")


def test_apply_tier_override_rewrites_all_tiers_preserving_names(tmp_path):
    from campaign_runner.sandbox import Sandbox
    from campaign_runner.plan import TierOverride
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 1}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    spec = tmp_path / "ws.yml"
    spec.write_text(textwrap.dedent("""
        name: wf
        version: "1.0"
        model_tiers:
          small: {provider: anthropic, model: claude-haiku, api_key_env: ANTHROPIC_API_KEY, temperature: 0.3, max_tokens: 1024}
          large: {provider: ollama, model: llama, api_key_env: ""}
        role_type_defaults: {worker: small, judge: large}
        stages: [{id: s1, role: {name: S, type: worker, description: x}, output_mode: text, depends_on: []}]
    """).strip() + "\n")
    ov = TierOverride(apply=True, tiers={"tiny": {"provider": "openrouter",
            "model": "qwen/qwen3.6-27b", "api_key_env": "OPENROUTER_API_KEY",
            "temperature": 0.2, "max_tokens": 512}})
    sb.apply_tier_override(spec, ov)
    import yaml
    parsed = yaml.safe_load(spec.read_text())
    assert set(parsed["model_tiers"].keys()) == {"small", "large"}   # names preserved
    for v in parsed["model_tiers"].values():
        assert v["model"] == "qwen/qwen3.6-27b"
        assert v["provider"] == "openrouter"
    # idempotent
    sb.apply_tier_override(spec, ov)
    assert yaml.safe_load(spec.read_text()) == parsed


def test_apply_tier_override_noop_when_no_override(tmp_path):
    from campaign_runner.sandbox import Sandbox
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 1}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    spec = tmp_path / "ws.yml"
    spec.write_text("name: wf\nmodel_tiers: {small: {provider: anthropic, model: x}}\n")
    before = spec.read_text()
    sb.apply_tier_override(spec, None)
    assert spec.read_text() == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_apply_tier_override_rewrites_all_tiers_preserving_names -q`
Expected: FAIL — `AttributeError: 'Sandbox' object has no attribute 'apply_tier_override'`.

- [ ] **Step 3: Implement** (add to `sandbox.py`, inside `class Sandbox`)

```python
    def working_spec_for(self, phase_id: str) -> Path:
        return self.dir / f"spec_work_{phase_id}.yml"

    def copy_working_spec_to(self, source: Path, target: Path) -> Path:
        shutil.copyfile(source, target)
        return target

    def apply_tier_override(self, spec_path: Path, override) -> None:
        """Rewrite every entry in spec's model_tiers to the override's tiny tier,
        preserving tier names so stage model_tier references still resolve.
        Idempotent. No-op when override is None/disabled/has no tiny tier/spec has
        no model_tiers."""
        import yaml
        if override is None or not override.apply or not override.tiers:
            return
        tiny = override.tiers.get("tiny") or next(iter(override.tiers.values()))
        spec = yaml.safe_load(spec_path.read_text()) or {}
        tiers = spec.get("model_tiers") or {}
        if not tiers:
            return
        for name in list(tiers.keys()):
            tiers[name] = dict(tiny)            # preserve name, swap config
        spec["model_tiers"] = tiers
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/sandbox.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): sandbox per-phase working specs + tier override

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: runner.py — per-phase workflow resolution + tier override

**Files:**
- Modify: `experiments/campaign/campaign_runner/runner.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `CampaignRunner._resolve_phase_spec(phase) -> Path` (phase.workflow or plan.workflow, resolved against `HARNESS_ROOT`, falling back to `tests/fixtures/sample_spec.yml`); per-phase copy to `sb.working_spec_for(phase.id)`; per-phase `workflow_name` parsed from the copied spec; `self.last_working_spec` updated each phase.
- Consumes: `Sandbox.working_spec_for`/`copy_working_spec_to`/`apply_tier_override` (Task 2).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_per_phase_workflow_resolution_and_tier_override(tmp_path, monkeypatch):
    from campaign_runner import runner
    # two self-contained workflow specs with distinct names + tiers
    wa = tmp_path / "wf_a.yml"
    wa.write_text('name: wf-a\nversion: "1.0"\nmodel_tiers:\n'
                  '  small: {provider: anthropic, model: claude-haiku, api_key_env: ANTHROPIC_API_KEY}\n'
                  'role_type_defaults: {worker: small}\n'
                  'contracts: {inputs: [{name: topic}]}\n'
                  'stages: [{id: s, role: {name: S, type: worker, description: "{{ topic }}"}, output_mode: text, depends_on: []}]\n')
    wb = tmp_path / "wf_b.yml"
    wb.write_text('name: wf-b\nversion: "1.0"\nmodel_tiers:\n'
                  '  small: {provider: ollama, model: llama, api_key_env: ""}\n'
                  'role_type_defaults: {worker: small}\n'
                  'contracts: {inputs: [{name: topic}]}\n'
                  'stages: [{id: s, role: {name: S, type: worker, description: "{{ topic }}"}, output_mode: text, depends_on: []}]\n')
    plan_p = tmp_path / "plan.yml"
    plan_p.write_text(textwrap.dedent(f"""
        name: soak-res
        description: x
        workflow: {wa}
        tier_override:
          apply: true
          tiers:
            tiny: {{provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}}
        budget: {{max_runs: 5}}
        phases:
          - {{id: a, lever: none, inputs: {{topic: "q"}}, repeats: 1}}
          - {{id: b, lever: none, workflow: {wb}, inputs: {{topic: "q"}}, repeats: 1}}
        verdicts: {{}}
    """))
    plan = load_plan(plan_p)

    captured = []
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p):
            captured.append(("validate", str(p)))
            return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            captured.append(("run", str(spec), workflow_name))
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms,success,output_valid) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("r1", workflow_name, "s", "worker", "m", "2026-01-01T00:00:01", 100.0, 1, 1))
            con.commit(); con.close()
            from campaign_runner import cli_driver
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"},
                                         hqs_armature={"authoritative": 0.8, "dashboard": None} if tag == "main" else None)
        def improve(self, spec, **kw): ...
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)

    r = runner.CampaignRunner(plan, wa, root=tmp_path / "out")
    result = r.run()
    # two distinct per-phase working specs, distinct workflow names, tiers overridden to openrouter
    specs = [c[1] for c in captured if c[0] == "validate"]
    assert len(set(specs)) == 2
    import yaml
    for sp in set(specs):
        parsed = yaml.safe_load(Path(sp).read_text())
        assert parsed["model_tiers"]["small"]["provider"] == "openrouter"
    names = [c[2] for c in captured if c[0] == "run"]
    assert "wf-a" in names and "wf-b" in names
    assert len(result.rows) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_per_phase_workflow_resolution_and_tier_override -q`
Expected: FAIL — runner still uses the single `self.sb.working_spec` / `self.workflow_name`.

- [ ] **Step 3: Implement** (edit `runner.py`)

Add `HARNESS_ROOT` near the top imports:
```python
from pathlib import Path
HARNESS_ROOT = Path(__file__).resolve().parent.parent   # experiments/campaign/
```

In `__init__`, after `self.drv = CliDriver(...)`, add:
```python
        self.last_working_spec = self.sb.working_spec
```

Add two helpers after `_workflow_name`:
```python
    def _resolve_phase_spec(self, phase) -> Path:
        src = phase.workflow or self.plan.workflow
        p = Path(src)
        if not p.is_absolute():
            p = HARNESS_ROOT / src
        if not p.exists():
            p = HARNESS_ROOT / "tests" / "fixtures" / "sample_spec.yml"
        return p

    def _phase_workflow_name(self, ws: Path) -> str:
        import yaml
        try:
            return (yaml.safe_load(ws.read_text()) or {}).get("name", "")
        except Exception:
            return ""
```

In `run()`, replace the body of the `for pi, phase in enumerate(...)` loop's **pre-rep** section. The new loop head (concurrency branch added in Task 7; for now handle non-concurrency):
```python
        for pi, phase in enumerate(self.plan.phases):
            if phase.fresh_db:
                self.sb.reset_trace_db()
            if phase.concurrency is not None:
                # Task 7 fills this in; keep the serial path inactive here.
                gaps.append({"want": "concurrency phase", "needed": "not yet implemented",
                             "severity": "high", "phase": phase.id})
                continue
            ws = self.sb.working_spec_for(phase.id)
            self.sb.copy_working_spec_to(self._resolve_phase_spec(phase), ws)
            if self.plan.tier_override:
                self.sb.apply_tier_override(ws, self.plan.tier_override)
            self.last_working_spec = ws
            wf_name = self._phase_workflow_name(ws)
            if not self.drv.validate(ws):
                gaps.append({"want": "valid spec after tier override", "needed": "validate exit 0",
                             "severity": "high", "phase": phase.id})
                continue
            for rep in range(phase.repeats):
                if self._budget_exceeded(len(rows), llm_calls, time.monotonic() - t0,
                                         trace_io.total_tokens(self.sb.trace_db)):
                    gaps.append({"want": "budget", "needed": "stop before max_runs/llm/wallclock",
                                 "severity": "info"})
                    break
                spec_before = ws.read_text()
                inputs = fault.apply_lever(phase, phase_index=pi, rep=rep, corpus=corpus,
                                           working_spec=ws, rng_seed=1000)
                out = self.drv.run(ws, inputs, workflow_name=wf_name,
                                   meta={"phase_id": phase.id, "lever": phase.lever,
                                         "inputs": inputs})
                llm_calls += 1
                improve_log, recovery, spec_diff = [], None, ""
                if phase.self_improve and phase.self_improve.enabled:
                    improve_log, recovery, improve_llm = self._do_improve(phase, inputs, gaps)
                    llm_calls += improve_llm
                    spec_diff = self._diff(spec_before, ws.read_text())
                rows.append(self._row_from_run(out.run_id, phase.id, phase.lever, inputs,
                                               out.exit_code, improve_log, recovery,
                                               spec_diff, self._memory_mode(),
                                               run_stderr=out.stderr, gaps=gaps,
                                               hqs_arm=out.hqs_armature))
                if improve_log:
                    rows[-1]["hqs_armature"]["rolling"] = improve_log[-1].get("hqs_before")
        return self._finalize(rows, gaps)
```

In `_finalize`, change the `_spec_tiers(...)` call to use `self.last_working_spec`:
```python
                      "tiers": _spec_tiers(self.last_working_spec),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Run full suite (no regressions)**

Run: `cd experiments/campaign && python -m pytest -q`
Expected: PASS (70 passed).

- [ ] **Step 6: Commit**

```bash
git add experiments/campaign/campaign_runner/runner.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): per-phase workflow resolution + tier override in runner

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: soak_verdicts.py — reliability verdicts

**Files:**
- Create: `experiments/campaign/campaign_runner/soak_verdicts.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `all_soak_verdicts(rows: list[dict], plan, trace_db: Path | None = None) -> list[tuple[str,str,dict]]` and the 8 verdict functions. Each returns `(name, status, detail)` with status in `{PASS, FAIL, INCONCLUSIVE}`. `LLM_ROLE_TYPES = {"worker","researcher","judge","orchestrator"}`. Soak rows exclude rows with `is_concurrency_summary=True`. `no_row_loss_under_concurrency` derives `expected` from the plan's concurrency phase (`workers * reps_per_worker`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def _soak_row(run_id, hqs=0.8, exit_code=0, latency_ms=100.0, role="worker"):
    return {"run_id": run_id, "phase_id": "p", "lever": "none", "inputs": {},
            "exit_code": exit_code, "is_concurrency_summary": False,
            "hqs_ours": {"authoritative": hqs, "rolling": None, "dashboard": None, "feedback": None},
            "hqs_armature": {"authoritative": None, "rolling": None, "dashboard": None, "feedback": None},
            "improve_log": [], "recovery_hqs_ours": None, "spec_diff": "", "memory_mode": None,
            "_latency_ms": latency_ms, "_role": role}


def test_verdict_no_unclean_exits(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [_soak_row("r1", exit_code=0), _soak_row("r2", exit_code=1)]
    name, status, detail = sv.verdict_no_unclean_exits(rows, {"allowed_failures": 0})
    assert (name, status) == ("no_unclean_exits", "FAIL")
    assert detail["n_unclean"] == 1


def test_verdict_trace_db_integrity(tmp_path):
    import sqlite3
    from campaign_runner import soak_verdicts as sv
    db = tmp_path / "traces.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms) VALUES (?,?,?,?,?,?,?)",
                ("r1", "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0))
    con.commit(); con.close()
    name, status, detail = sv.verdict_trace_db_integrity([], {}, db)
    assert status == "PASS" and detail["integrity_check"] == "ok" and detail["n_null_run_id"] == 0


def test_verdict_no_row_loss_pass(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [{"is_concurrency_summary": True, "worker": 0, "run_ids": ["a", "b"],
             "exit_codes": [0], "n_trace_rows": 2, "sqlite_busy_count": 0},
            {"is_concurrency_summary": True, "worker": 1, "run_ids": ["c", "d"],
             "exit_codes": [0], "n_trace_rows": 2, "sqlite_busy_count": 0}]
    name, status, detail = sv.verdict_no_row_loss_under_concurrency(rows, {"expected": 4, "tolerance": 0})
    assert (name, status) == ("no_row_loss_under_concurrency", "PASS")
    assert detail["actual"] == 4 and detail["sqlite_busy_count"] == 0


def test_verdict_no_row_loss_busy_fails(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [{"is_concurrency_summary": True, "worker": 0, "run_ids": ["a"],
             "exit_codes": [0], "n_trace_rows": 1, "sqlite_busy_count": 1}]
    name, status, detail = sv.verdict_no_row_loss_under_concurrency(rows, {"expected": 4, "tolerance": 0})
    assert status == "FAIL" and detail["sqlite_busy_count"] == 1


def test_verdict_hqs_stability_no_drift(tmp_path):
    from campaign_runner import soak_verdicts as sv
    flat = [_soak_row(f"r{i}", hqs=0.80 + (i % 3) * 0.001) for i in range(20)]
    name, status, detail = sv.verdict_hqs_stability_no_drift(flat, {"max_mean_delta": 0.08})
    assert status == "PASS" and detail["delta"] <= 0.08
    drifty = [_soak_row(f"r{i}", hqs=0.5 + i * 0.05) for i in range(20)]
    _, status2, _ = sv.verdict_hqs_stability_no_drift(drifty, {"max_mean_delta": 0.08})
    assert status2 == "FAIL"


def test_verdict_wallclock_stability(tmp_path):
    import sqlite3
    from campaign_runner import soak_verdicts as sv
    db = tmp_path / "traces.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    rows = []
    for i in range(10):
        rid = f"r{i}"
        rows.append(_soak_row(rid, latency_ms=100.0 + i * 0.5))   # ~0.5 ms/run slope -> PASS
        con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms) VALUES (?,?,?,?,?,?,?)",
                    (rid, "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0 + i * 0.5))
    con.commit(); con.close()
    name, status, detail = sv.verdict_wallclock_stability(rows, {"max_latency_slope_ms_per_run": 5.0}, db)
    assert status == "PASS" and detail["slope_ms_per_run"] <= 5.0


def test_verdict_checkpoint_resume_correctness(tmp_path):
    from campaign_runner import soak_verdicts as sv
    rows = [_soak_row(f"r{i}") for i in range(5)]
    name, status, detail = sv.verdict_checkpoint_resume_correctness(rows, {"require_distinct_run_ids": True})
    assert status == "PASS" and detail["n_distinct_run_ids"] == 5
    dup = [_soak_row("r1"), _soak_row("r1")]
    _, status2, _ = sv.verdict_checkpoint_resume_correctness(dup, {"require_distinct_run_ids": True})
    assert status2 == "FAIL"


def test_verdict_budget_obeyed(tmp_path):
    from campaign_runner import soak_verdicts as sv
    plan = type("P", (), {"budget": type("B", (), {"max_runs": 5})(), "soak_verdicts": None})()
    rows = [_soak_row(f"r{i}") for i in range(4)]
    name, status, detail = sv.verdict_budget_obeyed(rows, {}, plan)
    assert status == "PASS" and detail["stop_reason"] == "completed"
    rows2 = [_soak_row(f"r{i}") for i in range(5)]
    _, status2, detail2 = sv.verdict_budget_obeyed(rows2, {}, plan)
    assert status2 == "PASS" and detail2["stop_reason"] == "budget"


def test_verdict_agent_spawn_count(tmp_path):
    import sqlite3
    from campaign_runner import soak_verdicts as sv
    db = tmp_path / "traces.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    for i in range(10):
        con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp) VALUES (?,?,?,?,?,?)",
                    (f"r{i}", "wf", "s", "worker", "m", "2026-01-01T00:00:01"))
    con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp) VALUES (?,?,?,?,?,?)",
                ("rt", "wf", "st", "tool_call", "m", "2026-01-01T00:00:01"))  # excluded
    con.commit(); con.close()
    name, status, detail = sv.verdict_agent_spawn_count([], {"min_total": 10}, db)
    assert status == "PASS" and detail["total_agents"] == 10
    _, status2, _ = sv.verdict_agent_spawn_count([], {"min_total": 100}, db)
    assert status2 == "FAIL"


def test_all_soak_verdicts_dispatcher(tmp_path):
    from campaign_runner import soak_verdicts as sv
    from campaign_runner.plan import load_plan
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-t
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases:
          - {id: a, lever: none, inputs: {topic: q}, repeats: 1}
          - id: b
            lever: none
            inputs: {topic: q}
            repeats: 1
            concurrency: {workers: 2, driver: armature_loop, reps_per_worker: 5}
        soak_verdicts: {no_unclean_exits: {allowed_failures: 0}, agent_spawn_count: {min_total: 1}}
        verdicts: {}
    """))
    plan = load_plan(p)
    rows = [_soak_row(f"r{i}") for i in range(3)]
    vs = sv.all_soak_verdicts(rows, plan, None)
    names = [v[0] for v in vs]
    assert "no_unclean_exits" in names and "agent_spawn_count" in names
    # expected derived from concurrency phase: 2 workers * 5 reps = 10
    nrl = [v for v in vs if v[0] == "no_row_loss_under_concurrency"][0]
    assert nrl[2]["expected"] == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: FAIL — `ModuleNotFoundError: campaign_runner.soak_verdicts`.

- [ ] **Step 3: Implement** (`soak_verdicts.py`, create)

```python
"""Reliability verdicts for the soak test. Each returns (name, status, detail).

Statuses: PASS / FAIL / INCONCLUSIVE. INCONCLUSIVE means the run did not
exercise the signal (e.g. no concurrency phase) — an observability note,
not a quiet pass.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

PASS, FAIL, INCON = "PASS", "FAIL", "INCONCLUSIVE"

LLM_ROLE_TYPES = {"worker", "researcher", "judge", "orchestrator"}


def _soak_rows(rows):
    return [r for r in rows if not r.get("is_concurrency_summary")]


def verdict_no_unclean_exits(rows, th):
    sr = _soak_rows(rows)
    allowed = th.get("allowed_failures", 0)
    bad = [r for r in sr if r.get("exit_code") not in (0, None)]
    ok = len(bad) <= allowed
    return ("no_unclean_exits", PASS if ok else FAIL,
            {"n_runs": len(sr), "n_unclean": len(bad),
             "allowed_failures": allowed,
             "bad_run_ids": [r.get("run_id") for r in bad]})


def verdict_trace_db_integrity(rows, th, trace_db):
    if not trace_db or not Path(trace_db).exists():
        return ("trace_db_integrity", INCON, {"n_rows": 0, "note": "no trace db"})
    try:
        con = sqlite3.connect(str(trace_db))
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        n_null = con.execute("SELECT count(*) FROM traces WHERE run_id IS NULL").fetchone()[0]
        n_rows = con.execute("SELECT count(*) FROM traces").fetchone()[0]
        con.close()
    except Exception as e:
        return ("trace_db_integrity", FAIL, {"error": str(e)})
    allow_null = th.get("allow_null_run_id", 0)
    ok = integrity == "ok" and n_null <= allow_null
    return ("trace_db_integrity", PASS if ok else FAIL,
            {"integrity_check": integrity, "n_null_run_id": n_null, "n_rows": n_rows})


def verdict_no_row_loss_under_concurrency(rows, th):
    conc = [r for r in rows if r.get("is_concurrency_summary")]
    if not conc:
        return ("no_row_loss_under_concurrency", INCON, {"n_concurrency_workers": 0})
    expected = th.get("expected", 0)
    tol = th.get("tolerance", 0)
    all_ids = [rid for r in conc for rid in (r.get("run_ids") or [])]
    actual = len(set(all_ids))
    busy = sum(r.get("sqlite_busy_count", 0) for r in conc)
    all_exit0 = all(r.get("exit_codes") and all(c == 0 for c in r["exit_codes"]) for r in conc)
    ok = busy == 0 and all_exit0 and abs(actual - expected) <= tol
    return ("no_row_loss_under_concurrency", PASS if ok else FAIL,
            {"expected": expected, "actual": actual, "sqlite_busy_count": busy,
             "per_worker_rows": [r.get("n_trace_rows") for r in conc]})


def verdict_hqs_stability_no_drift(rows, th):
    sr = _soak_rows(rows)
    vals = [(r.get("hqs_ours") or {}).get("authoritative") for r in sr]
    vals = [v for v in vals if v is not None]
    if len(vals) < 8:
        return ("hqs_stability_no_drift", INCON, {"n": len(vals)})
    n = len(vals)
    q = max(1, n // 4)
    q1, q4 = vals[:q], vals[-q:]
    m1, m4 = sum(q1) / len(q1), sum(q4) / len(q4)
    delta = abs(m1 - m4)
    ok = delta <= th.get("max_mean_delta", 0.08)
    return ("hqs_stability_no_drift", PASS if ok else FAIL,
            {"q1_mean": round(m1, 4), "q4_mean": round(m4, 4),
             "delta": round(delta, 4), "n": n})


def verdict_wallclock_stability(rows, th, trace_db):
    sr = _soak_rows(rows)
    ids = [r.get("run_id") for r in sr if r.get("run_id")]
    if len(ids) < 8 or not trace_db or not Path(trace_db).exists():
        return ("wallclock_stability", INCON, {"n": len(ids)})
    try:
        con = sqlite3.connect(str(trace_db))
        means = []
        for rid in ids:
            v = con.execute("SELECT avg(latency_ms) FROM traces WHERE run_id=?", (rid,)).fetchone()[0]
            means.append(v or 0.0)
        con.close()
    except Exception as e:
        return ("wallclock_stability", FAIL, {"error": str(e)})
    n = len(means)
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(means) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, means))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den if den else 0.0
    ok = slope <= th.get("max_latency_slope_ms_per_run", 5.0)
    return ("wallclock_stability", PASS if ok else FAIL,
            {"slope_ms_per_run": round(slope, 4), "n": n})


def verdict_checkpoint_resume_correctness(rows, th):
    sr = _soak_rows(rows)
    ids = [r.get("run_id") for r in sr if r.get("run_id")]
    if not ids:
        return ("checkpoint_resume_correctness", INCON, {"n_runs": 0})
    distinct = len(set(ids))
    dups = sorted({rid for rid in ids if ids.count(rid) > 1})
    require_distinct = th.get("require_distinct_run_ids", True)
    ok = (not require_distinct) or (distinct == len(ids))
    return ("checkpoint_resume_correctness", PASS if ok else FAIL,
            {"n_runs": len(ids), "n_distinct_run_ids": distinct, "dup_run_ids": dups})


def verdict_budget_obeyed(rows, th, plan):
    sr = _soak_rows(rows)
    mr = plan.budget.max_runs
    ok = len(sr) <= mr
    stop = "budget" if len(sr) >= mr else "completed"
    return ("budget_obeyed", PASS if ok else FAIL,
            {"n_rows": len(sr), "max_runs": mr, "stop_reason": stop})


def verdict_agent_spawn_count(rows, th, trace_db):
    if not trace_db or not Path(trace_db).exists():
        return ("agent_spawn_count", INCON, {"total_agents": 0})
    try:
        con = sqlite3.connect(str(trace_db))
        qmarks = ",".join(f"'{t}'" for t in LLM_ROLE_TYPES)
        total = con.execute(
            f"SELECT count(*) FROM traces WHERE role_type IN ({qmarks})").fetchone()[0]
        con.close()
    except Exception as e:
        return ("agent_spawn_count", FAIL, {"error": str(e)})
    mn = th.get("min_total", 5000)
    ok = total >= mn
    return ("agent_spawn_count", PASS if ok else FAIL,
            {"total_agents": total, "min_total": mn})


def all_soak_verdicts(rows, plan, trace_db=None):
    if plan.soak_verdicts is None:
        return []
    sv = plan.soak_verdicts
    expected = 0
    for ph in plan.phases:
        if ph.concurrency is not None:
            expected = ph.concurrency.workers * ph.concurrency.reps_per_worker
            break
    th_nrl = dict(sv.no_row_loss_under_concurrency)
    th_nrl.setdefault("expected", expected)
    return [
        verdict_no_unclean_exits(rows, sv.no_unclean_exits),
        verdict_trace_db_integrity(rows, sv.trace_db_integrity, trace_db),
        verdict_no_row_loss_under_concurrency(rows, th_nrl),
        verdict_hqs_stability_no_drift(rows, sv.hqs_stability_no_drift),
        verdict_wallclock_stability(rows, sv.wallclock_stability, trace_db),
        verdict_checkpoint_resume_correctness(rows, sv.checkpoint_resume_correctness),
        verdict_budget_obeyed(rows, sv.budget_obeyed, plan),
        verdict_agent_spawn_count(rows, sv.agent_spawn_count, trace_db),
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: PASS (17 passed).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/soak_verdicts.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): soak reliability verdicts module

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: runner.py — route soak verdicts in _finalize + write meta.json

**Files:**
- Modify: `experiments/campaign/campaign_runner/runner.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `_finalize` emits **only** soak verdicts when `plan.soak_verdicts` is set (skipping H1–H4), else the existing H1–H4. Also writes `self.sb.dir/meta.json` = `{name, purpose, date, git_sha, totals, verdict_statuses, report}` for the index page (Task 9).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_finalize_emits_soak_verdicts_when_set(tmp_path, monkeypatch):
    from campaign_runner import runner
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-fin
        description: x
        purpose: "soak purpose"
        workflow: specs/soak/synth_fanout_mid.yml
        budget: {max_runs: 5}
        phases: [{id: a, lever: none, inputs: {topic: q}, repeats: 1}]
        soak_verdicts: {agent_spawn_count: {min_total: 1}}
        verdicts: {}
    """))
    plan = load_plan(p)
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms,success,output_valid) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("r1", "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0, 1, 1))
            con.commit(); con.close()
            from campaign_runner import cli_driver
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"}, hqs_armature={"authoritative": 0.8, "dashboard": None})
        def improve(self, spec, **kw): ...
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)
    r = runner.CampaignRunner(plan, tmp_path / "ws.yml", root=tmp_path / "out")
    (tmp_path / "ws.yml").write_text('name: wf\nversion: "1.0"\nmodel_tiers: {small: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY}}\nrole_type_defaults: {worker: small}\nstages: [{id: s, role: {name: S, type: worker, description: x}, output_mode: text, depends_on: []}]\n')
    result = r.run()
    names = [v[0] for v in result.verdicts]
    assert "agent_spawn_count" in names
    assert "hqs_tracks_difficulty" not in names   # H1-H4 skipped for soak
    import json
    meta = json.loads((r.sb.dir / "meta.json").read_text())
    assert meta["purpose"] == "soak purpose"
    assert any(v["name"] == "agent_spawn_count" for v in meta["verdict_statuses"])


def test_finalize_emits_h1_h4_when_no_soak(tmp_path, monkeypatch):
    from campaign_runner import runner
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: h1-fin
        description: x
        workflow: specs/campaign_research_brief.yml
        budget: {max_runs: 5}
        phases: [{id: a, lever: none, inputs: {topic: q}, repeats: 1}]
        verdicts: {hqs_tracks_difficulty: {spearman_le: -0.5, p_le: 0.05}}
    """))
    plan = load_plan(p)
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb
        def validate(self, p): return True
        def run(self, spec, inputs, workflow_name="", tag="main", meta=None):
            import sqlite3
            con = sqlite3.connect(self.sb.trace_db)
            con.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
            con.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms,success,output_valid,quorum_score) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ("r1", "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0, 1, 1, 0.8))
            con.commit(); con.close()
            from campaign_runner import cli_driver
            return cli_driver.RunOutcome("r1", 0, "", "", {"run_id": "r1"}, hqs_armature={"authoritative": 0.8, "dashboard": None})
        def improve(self, spec, **kw): ...
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)
    r = runner.CampaignRunner(plan, tmp_path / "ws.yml", root=tmp_path / "out")
    (tmp_path / "ws.yml").write_text('name: wf\nversion: "1.0"\nmodel_tiers: {small: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY}}\nrole_type_defaults: {worker: small}\nstages: [{id: s, role: {name: S, type: worker, description: x}, output_mode: text, depends_on: []}]\n')
    result = r.run()
    names = [v[0] for v in result.verdicts]
    assert "hqs_tracks_difficulty" in names and "agent_spawn_count" not in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_finalize_emits_soak_verdicts_when_set -q`
Expected: FAIL — H1–H4 still emitted for soak; no meta.json.

- [ ] **Step 3: Implement** (edit `runner.py` `_finalize`)

Replace the verdict computation + report call block in `_finalize`:
```python
    def _finalize(self, rows: list[dict], gaps: list[dict]) -> CampaignResult:
        with open(self.campaign_jsonl, "w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        with open(self.gaps_jsonl, "w") as f:
            for g in gaps:
                f.write(json.dumps(g, default=str) + "\n")
        from campaign_runner import soak_verdicts
        if self.plan.soak_verdicts is not None:
            vs = soak_verdicts.all_soak_verdicts(rows, self.plan, self.sb.trace_db)
        else:
            vs = verdicts_mod.all_verdicts(rows, self.plan)
        date_str = _now()
        purpose = self.plan.purpose or self.plan.description
        meta = {"name": self.plan.name, "purpose": purpose, "date": date_str,
                "git_sha": _git_sha(),
                "totals": {"runs": len(rows), "phases": len(self.plan.phases)},
                "verdict_statuses": [{"name": n, "result": r} for n, r, _ in vs],
                "report": "report.html"}
        (self.sb.dir / "meta.json").write_text(json.dumps(meta, default=str))
        report = render_report(
            campaign={"name": self.plan.name, "description": self.plan.description,
                      "purpose": purpose, "git_sha": meta["git_sha"], "date": date_str,
                      "workflow": self.workflow_name,
                      "tiers": _spec_tiers(self.last_working_spec),
                      "totals": {"runs": len(rows), "phases": len(self.plan.phases)}},
            rows=rows, verdicts=vs, gaps=gaps,
            reproduce_cmd=f"python experiments/campaign/run.py {self.plan.name} "
                          f"--replay {self.recording.dir if self.recording else '<recording>'}",
            out_path=self.sb.dir / "report.html")
        return CampaignResult(rows=rows, verdicts=vs, gaps=gaps,
                              report_path=report, campaign_jsonl=self.campaign_jsonl,
                              gaps_jsonl=self.gaps_jsonl)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q && python -m pytest -q`
Expected: PASS (test_soak 19 passed; full suite green).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/runner.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): route soak verdicts + write meta.json for index page

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: concurrency.py — overlapping-firings stress

**Files:**
- Create: `experiments/campaign/campaign_runner/concurrency.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `run_workers(sb, spec_path: Path, conc: Concurrency, phase_id: str, recording=None) -> list[dict]`. Spawns `conc.workers` parallel `armature` subprocesses (driver `armature_loop` → `armature loop <spec> --max-runs reps_per_worker`; `armature_run_force` → `armature run <spec> --force --quiet`) all with `env=sb.env()` (shared `HOME` → shared `traces.db`). Returns one summary dict per worker: `{run_id: None, phase_id, lever:"none", is_concurrency_summary: True, worker, exit_code, exit_codes:[...], run_ids:[...], n_trace_rows:int, sqlite_busy_count:int, hqs_ours:null, hqs_armature:null, inputs:{}, improve_log:[], recovery_hqs_ours:null, spec_diff:"", memory_mode:null}`. Counts distinct run_ids added during the phase and distributes them across worker summaries; `sqlite_busy_count` from stderr scan for `"database is locked"` / `"sqlite_busy"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_run_workers_concurrent_writes_no_row_loss(tmp_path, monkeypatch):
    """N fake workers each insert reps_per_worker distinct rows into the shared
    WAL trace DB concurrently; run_workers must report zero BUSY and total
    distinct run_ids == workers*reps_per_worker."""
    import sqlite3, threading
    from campaign_runner import concurrency as conc_mod
    from campaign_runner.sandbox import Sandbox
    from campaign_runner.plan import load_plan, Concurrency
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 5}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    # initialize the trace DB schema (armature normally does this)
    con = sqlite3.connect(sb.trace_db)
    con.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY, run_id TEXT, workflow_name TEXT, stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, latency_ms REAL DEFAULT 0, success INTEGER DEFAULT 1, output_valid INTEGER DEFAULT 1, quorum_score REAL, timestamp TEXT, inputs_json TEXT DEFAULT '{}', outputs_json TEXT DEFAULT '{}', error_type TEXT, escalation_count INTEGER DEFAULT 0, spec_version TEXT DEFAULT '')")
    con.commit(); con.close()
    reps = 5
    class FakePopen:
        def __init__(self, cmd, env=None, stdout=None, stderr=None, text=False):
            self.cmd = cmd; self.returncode = 0
            self._out, self._err = "", ""
            db = Path(env["HOME"]) / ".armature" / "traces.db"
            self._t = threading.Thread(target=self._write, args=(db, reps))
            self._t.start()
        def _write(self, db, n):
            c = sqlite3.connect(db, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            for r in range(n):
                rid = f"wk-{threading.get_ident()}-{r}"
                c.execute("INSERT INTO traces (run_id,workflow_name,stage_id,role_type,model,timestamp,latency_ms,success,output_valid) VALUES (?,?,?,?,?,?,?,?,?)",
                          (rid, "wf", "s", "worker", "m", "2026-01-01T00:00:01", 100.0, 1, 1))
            c.commit(); c.close()
        def communicate(self):
            self._t.join(); return self._out, self._err
    monkeypatch.setattr(conc_mod.subprocess, "Popen", FakePopen)
    conc = Concurrency(workers=3, driver="armature_loop", reps_per_worker=reps)
    summaries = conc_mod.run_workers(sb, tmp_path / "spec.yml", conc, "overlap")
    assert len(summaries) == 3
    all_ids = [rid for s in summaries for rid in s["run_ids"]]
    assert len(set(all_ids)) == 3 * reps
    assert all(s["sqlite_busy_count"] == 0 for s in summaries)
    assert all(s["exit_code"] == 0 for s in summaries)
    assert all(s["is_concurrency_summary"] for s in summaries)


def test_run_workers_counts_busy_from_stderr(tmp_path, monkeypatch):
    from campaign_runner import concurrency as conc_mod
    from campaign_runner.sandbox import Sandbox
    from campaign_runner.plan import load_plan, Concurrency
    p = tmp_path / "plan.yml"
    p.write_text("name: t\ndescription: x\nworkflow: s.yml\nbudget: {max_runs: 5}\n"
                 "phases: [{id: a, lever: none, inputs: {}, repeats: 1}]\nverdicts: {}\n")
    sb = Sandbox(load_plan(p), root=tmp_path / "out")
    class FakePopen:
        def __init__(self, cmd, env=None, stdout=None, stderr=None, text=False):
            self.returncode = 1; self._out = ""; self._err = "Error: database is locked\n"
        def communicate(self): return self._out, self._err
    monkeypatch.setattr(conc_mod.subprocess, "Popen", FakePopen)
    conc = Concurrency(workers=2, driver="armature_run_force", reps_per_worker=1)
    summaries = conc_mod.run_workers(sb, tmp_path / "spec.yml", conc, "overlap")
    assert all(s["sqlite_busy_count"] >= 1 for s in summaries)
    assert all(s["exit_code"] == 1 for s in summaries)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_run_workers_concurrent_writes_no_row_loss -q`
Expected: FAIL — `ModuleNotFoundError: campaign_runner.concurrency`.

- [ ] **Step 3: Implement** (`concurrency.py`, create)

```python
"""Overlapping-firings concurrency stress: N parallel armature subprocesses
against ONE shared trace DB (HOME = sandbox dir). WAL has no busy-retry, so
overlapping writers risk SQLITE_BUSY / row loss — this module surfaces that
as observable summary rows; it does NOT retry (a BUSY crash is a finding)."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path


def _count_rows(db: Path) -> int:
    if not Path(db).exists():
        return 0
    try:
        con = sqlite3.connect(str(db))
        n = con.execute("SELECT count(*) FROM traces").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def _new_run_ids(db: Path, before: int) -> list[str]:
    """Return run_ids inserted after `before` rows existed (by row id order)."""
    if not Path(db).exists():
        return []
    try:
        con = sqlite3.connect(str(db))
        all_rows = con.execute(
            "SELECT run_id FROM traces WHERE run_id IS NOT NULL ORDER BY id").fetchall()
        con.close()
        return [r[0] for r in all_rows[before:]]
    except Exception:
        return []


def run_workers(sb, spec_path: Path, conc, phase_id: str, recording=None) -> list[dict]:
    """Spawn `workers` parallel armature subprocesses against the shared trace DB
    (HOME = sandbox dir). `armature_loop` runs `reps_per_worker` iterations in one
    process via --max-iterations; `armature_run_force` spawns `reps_per_worker`
    separate `armature run --force` processes per worker. Returns one summary dict
    per worker. Does NOT retry a BUSY crash — that is a finding to surface."""
    # Build a (worker_index, cmd) list. armature_loop: 1 cmd/worker (iterations via flag).
    # armature_run_force: reps_per_worker cmds/worker (one run each).
    cmds: list[tuple[int, list[str]]] = []
    for w in range(conc.workers):
        if conc.driver == "armature_loop":
            cmds.append((w, ["armature", "loop", str(spec_path),
                             "--max-iterations", str(conc.reps_per_worker), "--quiet"]))
        else:
            for _ in range(conc.reps_per_worker):
                cmds.append((w, ["armature", "run", str(spec_path), "--force", "--quiet"]))

    rows_before = _count_rows(sb.trace_db)
    procs = [subprocess.Popen(c, env=sb.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for (_w, c) in cmds]
    outs = [p.communicate() for p in procs]
    new_ids = _new_run_ids(sb.trace_db, rows_before)

    # group processes back into per-worker summaries
    summaries = []
    for w in range(conc.workers):
        idxs = [i for i, (wi, _c) in enumerate(cmds) if wi == w]
        w_procs = [procs[i] for i in idxs]
        w_outs = [outs[i] for i in idxs]
        exit_codes = [p.returncode for p in w_procs]
        busy = sum(o[1].lower().count("database is locked") + o[1].lower().count("sqlite_busy")
                   for o in w_outs)
        summaries.append({
            "run_id": None, "phase_id": phase_id, "lever": "none",
            "is_concurrency_summary": True, "worker": w,
            "exit_code": max(exit_codes) if exit_codes else 0,
            "exit_codes": exit_codes,
            "run_ids": [],           # filled below
            "n_trace_rows": 0,       # filled below
            "sqlite_busy_count": busy,
            "hqs_ours": None, "hqs_armature": None, "inputs": {},
            "improve_log": [], "recovery_hqs_ours": None,
            "spec_diff": "", "memory_mode": None,
        })

    # distribute new run_ids + new rows evenly across the worker summaries
    total_new = _count_rows(sb.trace_db) - rows_before
    per = total_new // len(summaries) if summaries else 0
    k = len(new_ids) // len(summaries) if summaries else 0
    for i, s in enumerate(summaries):
        s["run_ids"] = new_ids[i * k:(i + 1) * k] if len(summaries) > 1 else new_ids
        s["n_trace_rows"] = per

    if recording is not None:
        for s in summaries:
            recording.record_run(None, ["armature", conc.driver, str(spec_path)], "", "",
                                 s["exit_code"], [], {}, {}, tag="concurrency",
                                 meta={"summary": s})
    return summaries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q`
Expected: PASS (21 passed).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/concurrency.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): overlapping-firings concurrency stress module

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: runner.py — dispatch the concurrency phase

**Files:**
- Modify: `experiments/campaign/campaign_runner/runner.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: the `if phase.concurrency is not None:` branch in `run()` — resolves the phase spec, copies + tier-overrides it, validates, calls `concurrency.run_workers(...)`, extends `rows` with the summaries. `replay()` restores concurrency summary rows from recording entries tagged `concurrency`.
- Consumes: `concurrency.run_workers` (Task 6).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_concurrency_phase_dispatched_and_recorded(tmp_path, monkeypatch):
    from campaign_runner import runner, concurrency as conc_mod
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: soak-conc
        description: x
        workflow: specs/soak/synth_fanout_mid.yml
        tier_override:
          apply: true
          tiers:
            tiny: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
        budget: {max_runs: 20}
        phases:
          - id: overlap
            lever: none
            inputs: {topic: q}
            repeats: 1
            concurrency: {workers: 2, driver: armature_loop, reps_per_worker: 3}
        soak_verdicts: {no_unclean_exits: {allowed_failures: 0}}
        verdicts: {}
    """))
    plan = load_plan(p)
    fake_summaries = [{"run_id": None, "phase_id": "overlap", "lever": "none",
                       "is_concurrency_summary": True, "worker": i, "exit_code": 0,
                       "exit_codes": [0], "run_ids": [f"w{i}-0", f"w{i}-1", f"w{i}-2"],
                       "n_trace_rows": 3, "sqlite_busy_count": 0,
                       "hqs_ours": None, "hqs_armature": None, "inputs": {},
                       "improve_log": [], "recovery_hqs_ours": None,
                       "spec_diff": "", "memory_mode": None} for i in range(2)]
    called = {}
    class FakeDrv:
        def __init__(self, sb, rec): self.sb = sb; self.rec = rec
        def validate(self, p): return True
        def run(self, *a, **k): raise AssertionError("serial run should not be called for concurrency phase")
        def improve(self, *a, **k): ...
        def dashboard_json(self, w): return {}
        def replay_hqs(self, run_id): return 0.8
    def fake_run_workers(sb, spec_path, conc, phase_id, recording=None):
        called["n"] = conc.workers
        called["spec"] = str(spec_path)
        return fake_summaries
    monkeypatch.setattr(runner, "CliDriver", FakeDrv)
    monkeypatch.setattr(conc_mod, "run_workers", fake_run_workers)
    r = runner.CampaignRunner(plan, tmp_path / "ws.yml", root=tmp_path / "out", record_mode=True)
    (tmp_path / "ws.yml").write_text('name: synth-fanout-mid\nversion: "1.0"\nmodel_tiers: {small: {provider: anthropic, model: x, api_key_env: X}}\nrole_type_defaults: {worker: small}\nstages: [{id: s, role: {name: S, type: worker, description: x}, output_mode: text, depends_on: []}]\n')
    result = r.run()
    assert called.get("n") == 2
    assert len(result.rows) == 2
    assert all(row["is_concurrency_summary"] for row in result.rows)
    # replay reproduces the concurrency summary rows
    replayed = r.replay(r.recording.dir)
    assert len(replayed.rows) == 2
    assert all(row["is_concurrency_summary"] for row in replayed.rows)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_concurrency_phase_dispatched_and_recorded -q`
Expected: FAIL — concurrency branch currently logs a gap and continues (no summaries).

- [ ] **Step 3: Implement** (edit `runner.py`)

Replace the Task-3 placeholder concurrency branch (`if phase.concurrency is not None: gaps.append(...); continue`) with:
```python
            if phase.concurrency is not None:
                from campaign_runner import concurrency as conc_mod
                ws = self.sb.working_spec_for(phase.id)
                self.sb.copy_working_spec_to(self._resolve_phase_spec(phase), ws)
                if self.plan.tier_override:
                    self.sb.apply_tier_override(ws, self.plan.tier_override)
                self.last_working_spec = ws
                if not self.drv.validate(ws):
                    gaps.append({"want": "valid concurrency spec", "needed": "validate exit 0",
                                 "severity": "high", "phase": phase.id})
                    continue
                summaries = conc_mod.run_workers(self.sb, ws, phase.concurrency, phase.id,
                                                  self.recording)
                rows.extend(summaries)
                continue
```

In `replay()`, at the top of the `for r in rec.replay():` loop, add a concurrency branch before the existing `tag == "probe"` check:
```python
        for r in rec.replay():
            if r.get("tag") == "concurrency":
                rows.append((r.get("meta") or {}).get("summary", {}))
                continue
            tr = [trace_io.TraceRow(**t) for t in r["trace_rows"]]
            meta = r.get("meta") or {}
            if r.get("tag", "main") == "probe":
                ...   # unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q && python -m pytest -q`
Expected: PASS (test_soak 22; full suite green).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/runner.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): dispatch concurrency phase + restore summaries in replay

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: report.py — purpose paragraph + bottom narrative + soak metrics

**Files:**
- Modify: `experiments/campaign/campaign_runner/report.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `narrative(verdicts: list[tuple[str,str,dict]], rows, plan) -> str` (maps each verdict to good/bad/inconclusive + an overall line). `render_report` gains a `purpose` key in `campaign` (rendered as a "What this test is" paragraph at the top) and appends the narrative at the bottom. When `agent_spawn_count` verdict is present, a "Soak metrics" block renders its `total_agents`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_narrative_maps_verdict_statuses():
    from campaign_runner.report import narrative
    vs = [("no_unclean_exits", "PASS", {"n_runs": 3}),
          ("trace_db_integrity", "FAIL", {"n_null_run_id": 1}),
          ("hqs_stability_no_drift", "INCONCLUSIVE", {"n": 2})]
    html = narrative(vs, [], None)
    assert "no_unclean_exits" in html and "good" in html
    assert "trace_db_integrity" in html and "bad" in html
    assert "INCONCLUSIVE" in html or "inconclusive" in html
    assert "does NOT support" in html   # overall line when any FAIL


def test_render_report_includes_purpose_and_narrative(tmp_path):
    from campaign_runner.report import render_report
    out = tmp_path / "report.html"
    vs = [("agent_spawn_count", "PASS", {"total_agents": 60000, "min_total": 5000})]
    render_report(campaign={"name": "soak", "description": "d", "purpose": "Prove the engine stays up under cron.",
                            "git_sha": "abc", "date": "2026-06-27", "workflow": "w",
                            "tiers": [], "totals": {"runs": 500, "phases": 7}},
                  rows=[], verdicts=vs, gaps=[],
                  reproduce_cmd="python run.py soak --replay rec", out_path=out)
    html = out.read_text()
    assert "Prove the engine stays up under cron." in html
    assert "60,000" in html or "60000" in html   # soak metrics agent total
    assert "Overall" in html and "supports" in html   # narrative overall (all PASS)


def test_render_report_purpose_falls_back_to_description(tmp_path):
    from campaign_runner.report import render_report
    out = tmp_path / "report.html"
    render_report(campaign={"name": "x", "description": "desc-only", "purpose": "",
                            "git_sha": "a", "date": "d", "workflow": "w", "tiers": [], "totals": {}},
                  rows=[], verdicts=[], gaps=[], reproduce_cmd="c", out_path=out)
    html = out.read_text()
    assert "desc-only" in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_narrative_maps_verdict_statuses -q`
Expected: FAIL — `ImportError: cannot import name 'narrative'`.

- [ ] **Step 3: Implement** (edit `report.py`)

Add after `_verdict_rows_html`:
```python
def narrative(verdicts: list[tuple[str, str, dict]], rows: list[dict], plan) -> str:
    if not verdicts:
        return "<h2>Narrative</h2><p>(no verdicts recorded)</p>"
    statuses = [v[1] for v in verdicts]
    if any(s == "FAIL" for s in statuses):
        head = "Overall: this run does NOT support the stated purpose — at least one verdict FAILED."
    elif statuses and all(s == "PASS" for s in statuses):
        head = "Overall: this run supports the stated purpose — all verdicts PASS."
    else:
        head = "Overall: this run is INCONCLUSIVE for the stated purpose — one or more verdicts could not be settled."
    word = {"PASS": "good", "FAIL": "bad", "INCONCLUSIVE": "inconclusive"}
    items = "\n".join(
        f"<li><b>{escape(name)}</b>: {word.get(result, result)} — "
        f"<code>{escape(str(detail))}</code></li>"
        for name, result, detail in verdicts)
    return (f"<h2>Narrative</h2><p><b>{head}</b></p><ul>{items}</ul>")


def _soak_metrics_html(verdicts: list[tuple[str, str, dict]]) -> str:
    d = {n: detail for n, _r, detail in verdicts}
    asc = d.get("agent_spawn_count")
    if asc is None:
        return ""
    total = asc.get("total_agents", 0)
    return ("<h2>Soak metrics</h2><ul>"
            f"<li>Total agent spawns: <b>{total:,}</b> (min {asc.get('min_total', 5000):,})</li>"
            "</ul>")
```

In `render_report`, add `purpose` rendering + narrative + soak metrics. Change the body assembly (insert `purpose_html` after `desc_html`; insert `_soak_metrics_html(verdicts)` before the verdict table; append `narrative(...)` before the reproduce section):
```python
    purpose = campaign.get("purpose", "") or campaign.get("description", "")
    purpose_html = (f"<h2>What this test is</h2><p>{escape(purpose)}</p>"
                    if purpose else "")
    ...
    # inside the f-string body, after {desc_html}:
    {purpose_html}
    ...
    {_soak_metrics_html(verdicts)}
    <h2>Verdict table</h2>
    ...
    {narrative(verdicts, rows, campaign)}
    <h2>Reproduce this</h2>
```

(Add the three placeholders into the existing f-string at the indicated positions; keep all other sections unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q && python -m pytest -q`
Expected: PASS (test_soak 25; full suite green).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/report.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): report purpose paragraph, bottom narrative, soak metrics

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 9: report.py — index page + CLI --build-index

**Files:**
- Modify: `experiments/campaign/campaign_runner/report.py`
- Modify: `experiments/campaign/campaign_runner/cli.py`
- Test: `experiments/campaign/tests/test_soak.py` (append)

**Interfaces:**
- Produces: `build_index(out_dir: Path) -> Path` — scans `out/*/meta.json`, renders a self-contained `out/index.html` with one row per report (name, one-line purpose, date, overall verdict good/bad/inconclusive, link to its `report.html`). CLI: `python experiments/campaign/run.py --build-index <out_dir>` runs it and prints the path.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_soak.py`)

```python
def test_build_index_links_all_reports(tmp_path):
    import json
    from campaign_runner.report import build_index
    out = tmp_path / "out"
    for name, purpose, statuses in [
        ("soak", "Soak reliability test.", [("agent_spawn_count", "PASS")]),
        ("h1-five-level", "H1 HQS dynamics.", [("hqs_tracks_difficulty", "FAIL")]),
    ]:
        d = out / name
        d.mkdir(parents=True)
        (d / "report.html").write_text(f"<html>{name}</html>")
        (d / "meta.json").write_text(json.dumps(
            {"name": name, "purpose": purpose, "date": "2026-06-27",
             "verdict_statuses": [{"name": n, "result": r} for n, r in statuses]}))
    idx = build_index(out)
    html = idx.read_text()
    assert idx == out / "index.html"
    assert "soak" in html and "h1-five-level" in html
    assert "Soak reliability test." in html
    assert "soak/report.html" in html and "h1-five-level/report.html" in html
    assert "good" in html and "bad" in html   # soak all PASS -> good; h1 has FAIL -> bad
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py::test_build_index_links_all_reports -q`
Expected: FAIL — `ImportError: cannot import name 'build_index'`.

- [ ] **Step 3: Implement** (`report.py`, add)

```python
def build_index(out_dir: Path) -> Path:
    """Scan out/*/meta.json; render a self-contained out/index.html linking
    every campaign/soak report with name / purpose / date / overall verdict."""
    import json
    rows = []
    for meta_p in sorted(Path(out_dir).glob("*/meta.json")):
        try:
            m = json.loads(meta_p.read_text())
        except Exception:
            continue
        statuses = [v.get("result") for v in m.get("verdict_statuses", [])]
        if statuses and all(s == "PASS" for s in statuses):
            overall = "good"
        elif any(s == "FAIL" for s in statuses):
            overall = "bad"
        else:
            overall = "inconclusive"
        color = {"good": "#2e7d32", "bad": "#c62828", "inconclusive": "#f57c00"}[overall]
        rows.append(
            f"<tr><td><a href='{escape(meta_p.parent.name)}/report.html'>{escape(m.get('name', meta_p.parent.name))}</a></td>"
            f"<td>{escape(m.get('purpose', ''))}</td>"
            f"<td>{escape(m.get('date', ''))}</td>"
            f"<td style='color:{color}'>{overall}</td></tr>")
    body = "\n".join(rows) or "<tr><td colspan='4'>(no reports found)</td></tr>"
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Armature test reports</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:.4em .6em;text-align:left}}
h1{{border-bottom:2px solid #333}}</style></head>
<body><h1>Armature test reports</h1>
<p>Each row is one test run. Click a test name to open its full report (what it tests, the data,
verdicts, and a narrative of the results). Overall: <b>good</b> = all verdicts PASS,
<b>bad</b> = any FAIL, <b>inconclusive</b> = unsettled.</p>
<table><tr><th>Test</th><th>What it tests</th><th>Run</th><th>Overall</th></tr>
{body}</table></body></html>"""
    out = Path(out_dir) / "index.html"
    out.write_text(html)
    return out
```

Add the CLI flag in `cli.py` `main()` (after `args = ap.parse_args(argv)`, before `plan = load_plan(...)`):
```python
    ap.add_argument("--build-index", metavar="OUT_DIR",
                    help="build out/<index>.html linking all reports under OUT_DIR, then exit")
    args = ap.parse_args(argv)
    if args.build_index:
        from campaign_runner.report import build_index
        idx = build_index(Path(args.build_index))
        print(f"index -> {idx}")
        return 0
```
(Move the `--build-index` `add_argument` up next to the other `add_argument` calls; keep the handler block where shown.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd experiments/campaign && python -m pytest tests/test_soak.py -q && python -m pytest -q`
Expected: PASS (test_soak 26; full suite green).

- [ ] **Step 5: Commit**

```bash
git add experiments/campaign/campaign_runner/report.py experiments/campaign/campaign_runner/cli.py experiments/campaign/tests/test_soak.py
git commit -m "feat(campaign): report index page + --build-index CLI

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 10: workflow specs — 4 real copies + 3 synthetic fan-out

**Files:**
- Create: `experiments/campaign/specs/soak/real_research_pipeline.yml`, `real_deliberation.yml`, `real_competitive_analysis.yml`, `real_iterative_refinement.yml`, `synth_fanout_wide.yml`, `synth_fanout_deep.yml`, `synth_fanout_mid.yml`

**Interfaces:**
- Produces: 7 valid Armature workflow specs. The 4 real ones are verbatim copies of `examples/02_research_pipeline.yml`, `03_deliberation_standard.yml`, `04_fan_out.yml`, `11_iterative_refinement.yml` (tiers overridden at runtime by `tier_override`). The 3 synthetic ones are self-contained, tiny-tier, fan-out workflows requiring only a `topic` input.

- [ ] **Step 1: Copy the 4 real examples**

```bash
cd experiments/campaign
mkdir -p specs/soak
cp ../../examples/02_research_pipeline.yml specs/soak/real_research_pipeline.yml
cp ../../examples/03_deliberation_standard.yml specs/soak/real_deliberation.yml
cp ../../examples/04_fan_out.yml specs/soak/real_competitive_analysis.yml
cp ../../examples/11_iterative_refinement.yml specs/soak/real_iterative_refinement.yml
```

- [ ] **Step 2: Author `synth_fanout_wide.yml`** (planner → 40 workers → synthesizer)

```yaml
name: synth-fanout-wide
version: "1.0"
description: "Synthetic wide fan-out soak workflow: planner emits 40 items, fanned out to 40 parallel workers, then synthesized. ~40 agents/run."
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
  large: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
role_type_defaults: {worker: small, orchestrator: large, judge: large, researcher: small}
contracts:
  inputs: [{name: topic}]
  max_iterations: 40
  max_llm_calls: 200
stages:
  - id: planner
    role: {name: Planner, type: orchestrator, description: "Emit a JSON object with an `items` array of exactly 40 short research sub-questions about: {{ topic }}. Each item is {q: string}. Return only the object."}
    output_mode: guided_json
    output_schema:
      type: object
      required: [items]
      properties:
        items: {type: array, minItems: 40, maxItems: 40, items: {type: object, required: [q], properties: {q: {type: string}}}}
    depends_on: []
  - id: workers
    fan_out: 40
    fan_in: list
    partition_source: "{{ planner.items }}"
    partition_key: item
    role: {name: Worker, type: worker, description: "Answer in one concise sentence: {{ item.q }}"}
    output_mode: text
    depends_on: [planner]
  - id: synthesizer
    role: {name: Synthesizer, type: judge, description: "Summarize the 40 worker answers into one paragraph."}
    output_mode: text
    depends_on: [workers]
```

- [ ] **Step 3: Author `synth_fanout_deep.yml`** (planner_a → 15 workers_a → planner_b → 15 workers_b → synthesizer; depth-5, 30 agents)

```yaml
name: synth-fanout-deep
version: "1.0"
description: "Synthetic deep fan-out soak workflow: two sequential fan-out stages (15+15 workers) over a depth-5 DAG. ~30 agents/run."
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
  large: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
role_type_defaults: {worker: small, orchestrator: large, judge: large, researcher: small}
contracts:
  inputs: [{name: topic}]
  max_iterations: 40
  max_llm_calls: 200
stages:
  - id: planner_a
    role: {name: PlannerA, type: orchestrator, description: "Emit a JSON object with `items`: exactly 15 sub-questions about: {{ topic }}. Each item {q: string}."}
    output_mode: guided_json
    output_schema:
      type: object
      required: [items]
      properties:
        items: {type: array, minItems: 15, maxItems: 15, items: {type: object, required: [q], properties: {q: {type: string}}}}
    depends_on: []
  - id: workers_a
    fan_out: 15
    fan_in: list
    partition_source: "{{ planner_a.items }}"
    partition_key: item
    role: {name: WorkerA, type: worker, description: "Answer in one sentence: {{ item.q }}"}
    output_mode: text
    depends_on: [planner_a]
  - id: planner_b
    role: {name: PlannerB, type: orchestrator, description: "Given the prior worker answers, emit `items`: exactly 15 follow-up sub-questions about: {{ topic }}. Each item {q: string}."}
    output_mode: guided_json
    output_schema:
      type: object
      required: [items]
      properties:
        items: {type: array, minItems: 15, maxItems: 15, items: {type: object, required: [q], properties: {q: {type: string}}}}
    depends_on: [workers_a]
  - id: workers_b
    fan_out: 15
    fan_in: list
    partition_source: "{{ planner_b.items }}"
    partition_key: item
    role: {name: WorkerB, type: worker, description: "Answer in one sentence: {{ item.q }}"}
    output_mode: text
    depends_on: [planner_b]
  - id: synthesizer
    role: {name: Synthesizer, type: judge, description: "Synthesize all worker answers into one paragraph."}
    output_mode: text
    depends_on: [workers_b, workers_a]
```

- [ ] **Step 4: Author `synth_fanout_mid.yml`** (planner → 30 workers → synthesizer)

```yaml
name: synth-fanout-mid
version: "1.0"
description: "Synthetic mid fan-out soak workflow: planner emits 30 items, fanned out to 30 parallel workers, then synthesized. ~30 agents/run."
model_tiers:
  small: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
  large: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}
role_type_defaults: {worker: small, orchestrator: large, judge: large, researcher: small}
contracts:
  inputs: [{name: topic}]
  max_iterations: 40
  max_llm_calls: 200
stages:
  - id: planner
    role: {name: Planner, type: orchestrator, description: "Emit a JSON object with `items`: exactly 30 short sub-questions about: {{ topic }}. Each item {q: string}."}
    output_mode: guided_json
    output_schema:
      type: object
      required: [items]
      properties:
        items: {type: array, minItems: 30, maxItems: 30, items: {type: object, required: [q], properties: {q: {type: string}}}}
    depends_on: []
  - id: workers
    fan_out: 30
    fan_in: list
    partition_source: "{{ planner.items }}"
    partition_key: item
    role: {name: Worker, type: worker, description: "Answer in one concise sentence: {{ item.q }}"}
    output_mode: text
    depends_on: [planner]
  - id: synthesizer
    role: {name: Synthesizer, type: judge, description: "Summarize the 30 worker answers into one paragraph."}
    output_mode: text
    depends_on: [workers]
```

- [ ] **Step 5: Validate all 7 specs**

Run:
```bash
cd experiments/campaign
for f in specs/soak/*.yml; do echo "== $f =="; armature validate "$f" || echo "VALIDATE FAILED: $f"; done
```
Expected: each prints validation success (exit 0). If any real example fails validation (e.g. references a missing tool module), open it, note the failing field, and add the missing `tools:` module or fix the reference — do not silently skip. (The 4 examples already validate against the repo, so this should pass.)

- [ ] **Step 6: Commit**

```bash
git add experiments/campaign/specs/soak/
git commit -m "feat(campaign): soak workflow specs — 4 real examples + 3 synthetic fan-out

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 11: plans/soak.yml + h1 purpose

**Files:**
- Create: `experiments/campaign/plans/soak.yml`
- Modify: `experiments/campaign/plans/h1-five-level.yml` (add `purpose:`)

**Interfaces:**
- Produces: `plans/soak.yml` — 7 phases (4 real + 3 synthetic, ~500 reps total under budget) + 1 concurrency phase, `tier_override` pinned to `qwen/qwen3.6-27b`, `soak_verdicts` with the 8 thresholds, budget `{max_runs: 600, max_llm_calls: 15000, max_wallclock_hours: 3.0, max_tokens: 4000000}`. The H1 plan gains a `purpose:` so its reports pick up the paragraph + narrative on the next run.

- [ ] **Step 1: Author `plans/soak.yml`**

```yaml
name: soak
description: "Reliability/longevity soak: ~500 real runs across 7 workflows (~120 agents/iteration, ~60k agent spawns) on the tiny tier, plus an overlapping-firings concurrency phase against one shared trace DB."
purpose: >
  This is an endurance (soak) test of the Armature workflow engine. It runs seven
  workflows repeatedly under a cron-style budget to confirm the engine stays up and
  clean over many iterations: no crashes, no trace-DB corruption, no row loss when
  firings overlap, no HQS drift or latency creep across the run, correct
  checkpoint behavior on every --force rerun, and budget enforcement. It is NOT a
  correctness test of any workflow's output — only of the engine's reliability
  under load. An outsider can replay the recording at zero cost to verify the same
  verdicts. Memory-subsystem correctness is tested separately elsewhere.
workflow: specs/soak/synth_fanout_mid.yml
budget: {max_runs: 600, max_llm_calls: 15000, max_wallclock_hours: 3.0, max_tokens: 4000000}

tier_override:
  apply: true
  tiers:
    tiny: {provider: openrouter, model: qwen/qwen3.6-27b, api_key_env: OPENROUTER_API_KEY, temperature: 0.2, max_tokens: 512}

phases:
  # ── real workflows (credibility): ~20 agents/iteration each, ~80 reps → ~6400 spawns
  - {id: real_research,    lever: none, workflow: specs/soak/real_research_pipeline.yml,   inputs: {topic: "quantum error correction", audience: "engineers"}, repeats: 80}
  - {id: real_deliberation, lever: none, workflow: specs/soak/real_deliberation.yml,      inputs: {objective: "Should a startup adopt microservices?"}, repeats: 80}
  - {id: real_competitive, lever: none, workflow: specs/soak/real_competitive_analysis.yml, inputs: {company: "Acme", industry: "robotics"}, repeats: 80}
  - {id: real_iterative,   lever: none, workflow: specs/soak/real_iterative_refinement.yml, inputs: {topic: "explain CRDTs"}, repeats: 80}

  # ── synthetic fan-out (agent-spawn load): wide 40, deep 30, mid 30 → 100 agents/iteration
  - {id: synth_wide, lever: none, workflow: specs/soak/synth_fanout_wide.yml, inputs: {topic: "distributed systems"}, repeats: 60}
  - {id: synth_deep, lever: none, workflow: specs/soak/synth_fanout_deep.yml, inputs: {topic: "distributed systems"}, repeats: 60}
  - {id: synth_mid,  lever: none, workflow: specs/soak/synth_fanout_mid.yml,  inputs: {topic: "distributed systems"}, repeats: 60}

  # ── concurrency: 3 workers × 20 reps against one shared trace DB (WAL, no busy-retry)
  - id: cron_overlap
    lever: none
    workflow: specs/soak/synth_fanout_mid.yml
    inputs: {topic: "distributed systems"}
    repeats: 1
    concurrency: {workers: 3, driver: armature_loop, reps_per_worker: 20}

soak_verdicts:
  no_unclean_exits: {allowed_failures: 0}
  trace_db_integrity: {allow_null_run_id: 0}
  no_row_loss_under_concurrency: {tolerance: 0}
  hqs_stability_no_drift: {max_mean_delta: 0.08}
  wallclock_stability: {max_latency_slope_ms_per_run: 5.0}
  checkpoint_resume_correctness: {require_distinct_run_ids: true}
  budget_obeyed: {}
  agent_spawn_count: {min_total: 5000}

verdicts: {}
```

- [ ] **Step 2: Add `purpose:` to the H1 plan** (top of `plans/h1-five-level.yml`, after `description:`)

```yaml
purpose: >
  This is a hypothesis test of Armature's HQS dynamics. It checks three claims:
  (H1) HQS tracks input difficulty — harder corpora should score lower; (H2) the
  self-improve loop fires when a degradation lever drops HQS below target, applies
  a spec edit, and recovers above target; (H3) the harness's HQS formula reproduces
  Armature's independently emitted values. A fourth (H4) memory carry-forward
  hypothesis is inconclusive without cold/warm phases. An outsider can replay the
  recording at zero cost to verify the same verdicts.
```

- [ ] **Step 3: Validate the soak plan loads + dry-run a single rep**

Run:
```bash
cd experiments/campaign
python -c "from campaign_runner.plan import load_plan; p=load_plan('plans/soak.yml'); print('phases:', len(p.phases)); print('soak_verdicts:', p.soak_verdicts is not None)"
armature validate specs/soak/synth_fanout_mid.yml
```
Expected: `phases: 8`, `soak_verdicts: True`; validation exit 0.

- [ ] **Step 4: Commit**

```bash
git add experiments/campaign/plans/soak.yml experiments/campaign/plans/h1-five-level.yml
git commit -m "feat(campaign): soak plan + purpose on h1 plan

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 12: smoke run + full suite + index

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `cd experiments/campaign && python -m pytest -q`
Expected: PASS (all tests green, including the new `test_soak.py`).

- [ ] **Step 2: Smoke-run the soak plan at tiny scale** (override budget to ~6 runs, no concurrency blowup)

Create `/tmp/soak_smoke.yml` by copying `plans/soak.yml` and editing: set `budget: {max_runs: 6, max_llm_calls: 400, max_wallclock_hours: 0.25}`, every `repeats: 1`, and `concurrency: {workers: 2, driver: armature_loop, reps_per_worker: 1}`, `agent_spawn_count: {min_total: 1}`. Then:

```bash
cd experiments/campaign
set -a; . ./.env; set +a
python run.py /tmp/soak_smoke.yml --record --out-dir out
python run.py --build-index out
ls out/soak/report.html out/index.html out/soak/meta.json
```
Expected: prints `ran N runs -> .../soak/report.html`; `index.html` + `report.html` + `meta.json` exist. Open `out/index.html` in a browser to confirm it lists the soak report with a "good"/"bad"/"inconclusive" overall and a working link. Confirm `report.html` shows the "What this test is" paragraph at the top and a "Narrative" section at the bottom.

- [ ] **Step 3: Verify replay reproduces the smoke verdicts**

```bash
cd experiments/campaign
python run.py /tmp/soak_smoke.yml --replay out/soak/recording --out-dir out/soak-replay
```
Expected: prints `replayed N runs`; the replay's `report.html` shows the same verdict statuses as the live run.

- [ ] **Step 4: Regenerate the index over both the live H1 report and the soak smoke report**

```bash
cd experiments/campaign
python run.py --build-index out
```
Expected: `out/index.html` lists both `h1-five-level` and `soak` (if the H1 report's `meta.json` exists from a prior run; if not, run `python run.py plans/h1-five-level.yml --record --out-dir out` first to generate it).

- [ ] **Step 5: Final commit (smoke artifacts are gitignored under out/; nothing to add) — confirm branch state**

```bash
cd experiments/campaign
git status --short
git log --oneline feat/campaign-runner..feat/soak-test
```
Expected: clean working tree (apart from gitignored `out/` and `/tmp/soak_smoke.yml`); the log shows Tasks 1–11's commits.

- [ ] **Step 6: Report**

Tell the user: the soak harness is implemented and smoke-verified; full-suite green; the shareable landing page is `experiments/campaign/out/index.html`; the next step is the full ~500-run soak (Step 2's budget reset to the real `plans/soak.yml`), which takes ~2–3h and a few USD. Ask whether to launch the full run now.

---

## Self-Review (completed by plan author)

**Spec coverage:** Every spec section maps to a task — schema (T1), sandbox/override (T2), runner per-phase (T3), soak verdicts (T4), _finalize routing + meta.json (T5), concurrency module (T6), concurrency dispatch + replay (T7), report purpose/narrative/soak metrics (T8), index + CLI (T9), workflow specs (T10), soak plan + h1 purpose (T11), smoke (T12). Report UX (purpose/narrative/index) covered by T5 (meta.json) + T8 + T9. Concurrency summary-row schema + replay restoration covered by T6/T7. Tier-override-to-tiny covered by T2/T3.

**Placeholder scan:** No TBD/TODO/vague steps. Every code step contains the actual code. Step 6 of T10 has a guarded "if a real example fails validation, fix the reference" instruction — this is a concrete contingency with a defined action, not a placeholder (the examples already validate, so the branch is defensive).

**Type consistency:** `Concurrency`, `TierOverride`, `SoakVerdicts` field names match across T1 (definition), T2 (TierOverride consumed), T4 (SoakVerdicts consumed by `all_soak_verdicts`), T11 (YAML keys). `run_workers(sb, spec_path, conc, phase_id, recording)` signature matches T6 (defined) and T7 (called). `narrative(verdicts, rows, plan)` and `build_index(out_dir)` match T8/T9 and T9's CLI call. `working_spec_for`/`copy_working_spec_to`/`apply_tier_override` match T2/T3. `is_concurrency_summary`, `sqlite_busy_count`, `run_ids`, `n_trace_rows`, `exit_codes` keys are consistent across T6 (produced), T4 `no_row_loss` (consumed), T7 replay (restored).