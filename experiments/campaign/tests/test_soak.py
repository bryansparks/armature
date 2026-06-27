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