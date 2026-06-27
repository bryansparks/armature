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