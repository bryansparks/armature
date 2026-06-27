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