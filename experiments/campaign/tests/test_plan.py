from pathlib import Path
import textwrap
import pytest
from campaign_runner.plan import load_plan, CampaignPlan


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent(text))
    return p


def test_load_minimal_plan(tmp_path):
    p = _write(tmp_path, """
        name: hqdynamics-baseline
        description: "Headline trial."
        workflow: specs/dangerous_pretzel.yml
        budget: {max_runs: 5}
        phases:
          - id: ramp
            lever: input_difficulty_ramp
            inputs: {topic: "{{ corpus_row.topic }}"}
            repeats: 1
        verdicts: {}
    """)
    plan = load_plan(p)
    assert isinstance(plan, CampaignPlan)
    assert plan.name == "hqdynamics-baseline"
    assert plan.budget.max_runs == 5
    assert plan.phases[0].lever == "input_difficulty_ramp"
    assert plan.phases[0].repeats == 1
    assert plan.phases[0].self_improve is None


def test_self_improve_defaults_to_review_only(tmp_path):
    p = _write(tmp_path, """
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 1}
        phases:
          - id: p
            lever: none
            inputs: {}
            repeats: 1
            self_improve: {enabled: true}
        verdicts: {}
    """)
    plan = load_plan(p)
    si = plan.phases[0].self_improve
    assert si.enabled is True
    assert si.apply is False             # review-only by default (conservative)
    assert si.target_hqs == 0.75
    assert si.min_traces == 3
    assert si.max_rounds == 3


def test_rejects_unknown_lever(tmp_path):
    p = _write(tmp_path, """
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 1}
        phases:
          - id: p
            lever: bogus_lever
            inputs: {}
            repeats: 1
        verdicts: {}
    """)
    with pytest.raises(ValueError, match="unknown lever"):
        load_plan(p)