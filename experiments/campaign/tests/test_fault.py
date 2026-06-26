from pathlib import Path
import textwrap
import yaml
from campaign_runner import fault
from campaign_runner.plan import load_plan


def _plan_with_lever(tmp_path: Path, lever: str) -> tuple:
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent(f"""
        name: t
        description: "x"
        workflow: s.yml
        budget: {{max_runs: 9}}
        phases:
          - id: p
            lever: {lever}
            inputs: {{topic: "{{{{ corpus_row.topic }}}}", difficulty: "{{{{ corpus_row.level }}}}", seed: "{{{{ phase_index }}}}"}}
            repeats: 1
        verdicts: {{}}
    """))
    return load_plan(p), p


def test_input_difficulty_ramp_walks_corpus_in_order(tmp_path):
    plan, _ = _plan_with_lever(tmp_path, "input_difficulty_ramp")
    corpus = fault.load_corpus(Path(__file__).parent / "fixtures" / "difficulty.csv")
    # phase_index 0 -> first row, phase_index 1 -> second row
    inputs0 = fault.apply_lever(plan.phases[0], phase_index=0, rep=0,
                                 corpus=corpus, working_spec=tmp_path / "ws.yml", rng_seed=1)
    assert inputs0["topic"] == "quantum error correction"
    assert inputs0["difficulty"] == "1"
    inputs1 = fault.apply_lever(plan.phases[0], phase_index=1, rep=0,
                                 corpus=corpus, working_spec=tmp_path / "ws.yml", rng_seed=1)
    assert inputs1["topic"] == "protein folding dynamics"


def test_spec_corruption_mutates_working_spec_and_yields_seed(tmp_path):
    plan, _ = _plan_with_lever(tmp_path, "spec_corruption")
    ws = tmp_path / "spec_work.yml"
    ws.write_text(textwrap.dedent("""
        name: wf
        version: "1.0"
        stages:
          - id: researcher
            role: {name: Researcher, type: researcher, description: "Research cleanly."}
            output_mode: text
            depends_on: []
    """))
    before = ws.read_text()
    inputs = fault.apply_lever(plan.phases[0], phase_index=2, rep=0, corpus=[],
                               working_spec=ws, rng_seed=42)
    after = ws.read_text()
    assert after != before                      # corruption changed the spec
    assert "description:" in after              # still a parseable stage block
    assert "seed" in inputs


def test_none_lever_passes_inputs_through(tmp_path):
    plan, _ = _plan_with_lever(tmp_path, "none")
    inputs = fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                               working_spec=tmp_path / "ws.yml", rng_seed=1)
    # only the literal inputs from the plan (templated to empty for corpus refs)
    assert inputs["topic"] == ""