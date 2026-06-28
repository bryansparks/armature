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


def test_spec_corruption_unquoted_single_line(tmp_path):
    """Form 2: unquoted single-line description must be corrupted and stay parseable."""
    plan, _ = _plan_with_lever(tmp_path, "spec_corruption")
    ws = tmp_path / "spec_unquoted.yml"
    ws.write_text(textwrap.dedent("""
        name: wf
        version: "1.0"
        stages:
          - id: researcher
            role:
              name: Researcher
              type: researcher
              description: Research the topic cleanly.
            output_mode: text
            depends_on: []
    """))
    before = ws.read_text()
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=7)
    after = ws.read_text()
    assert after != before                       # corruption changed the spec
    parsed = yaml.safe_load(after)               # must still be parseable YAML
    assert parsed["name"] == "wf"
    # the description text was garbled
    desc = parsed["stages"][0]["role"]["description"]
    assert "ZZZCORRUPT" in desc


def test_spec_corruption_block_scalar(tmp_path):
    """Form 3: block-scalar description:| must be corrupted and stay parseable."""
    plan, _ = _plan_with_lever(tmp_path, "spec_corruption")
    ws = tmp_path / "spec_block.yml"
    ws.write_text(textwrap.dedent("""
        name: wf
        version: "1.0"
        stages:
          - id: researcher
            role:
              name: Researcher
              type: researcher
              description: |
                Research the topic thoroughly.
                Identify key findings and open questions.
            output_mode: text
            depends_on: []
    """))
    before = ws.read_text()
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=7)
    after = ws.read_text()
    assert after != before                       # corruption changed the spec
    parsed = yaml.safe_load(after)               # must still be parseable YAML
    assert parsed["name"] == "wf"
    # the block content was garbled
    desc = parsed["stages"][0]["role"]["description"]
    assert "ZZZCORRUPT" in desc


def test_none_lever_passes_inputs_through(tmp_path):
    plan, _ = _plan_with_lever(tmp_path, "none")
    inputs = fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                               working_spec=tmp_path / "ws.yml", rng_seed=1)
    # only the literal inputs from the plan (templated to empty for corpus refs)
    assert inputs["topic"] == ""


# ── memory_cold_warm lever (wires the H4 cold-vs-warm test) ──────────────

def _memory_plan(tmp_path: Path, memory_fresh: str) -> tuple:
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent(f"""
        name: t
        description: "x"
        workflow: s.yml
        budget: {{max_runs: 9}}
        phases:
          - id: p
            lever: memory_cold_warm
            inputs: {{memory_fresh: "{memory_fresh}"}}
            repeats: 1
        verdicts: {{}}
    """))
    return load_plan(p), p


def _memory_working_spec(tmp_path: Path, name: str = "ws.yml", fresh: str = "false") -> Path:
    """A spec with a memory block whose `fresh` the lever will toggle. The block
    also carries capture/inject_as — the lever must preserve those, only flip fresh.

    The researcher description is a block scalar with multi-line Jinja control
    blocks (`{% if %}` / `{{ _memory... }}`) — the exact shape that yaml.safe_dump
    round-trips into double-quoted folded form with stray backslashes, which breaks
    Jinja rendering at run time. The lever must preserve it verbatim."""
    ws = tmp_path / name
    ws.write_text(textwrap.dedent(f"""
        name: wf
        version: "1.0"
        memory:
          enabled: true
          fresh: {fresh}
          inject_as: _memory
          capture:
            - stage: researcher
              key: content
              max_entries: 8
        stages:
          - id: researcher
            role:
              name: Researcher
              type: researcher
              description: |
                Research the topic and produce a concise briefing.
                {{% if _memory is defined and _memory %}}
                Prior briefings (newest first). Build on these — refine, add new
                points not already covered, and ground new claims in what was
                established before. Do not merely repeat what is already there.
                {{{{ _memory.researcher.content }}}}
                {{% else %}}
                No prior briefings are available — produce a fresh briefing.
                {{% endif %}}
                Topic: {{{{ topic }}}}
            output_mode: text
            depends_on: []
    """))
    return ws


def test_memory_cold_warm_lever_sets_fresh_true_labels_cold(tmp_path):
    plan, _ = _memory_plan(tmp_path, "true")
    ws = _memory_working_spec(tmp_path)
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=1)
    spec = yaml.safe_load(ws.read_text())
    assert spec["memory"]["fresh"] is True          # lever wrote fresh=true
    assert fault.memory_mode(ws) == "cold"          # fresh truthy -> cold (ignores prior memory)


def test_memory_cold_warm_lever_sets_fresh_false_labels_warm(tmp_path):
    plan, _ = _memory_plan(tmp_path, "false")
    ws = _memory_working_spec(tmp_path, fresh="true")  # start warm-ready but fresh=true
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=1)
    spec = yaml.safe_load(ws.read_text())
    assert spec["memory"]["fresh"] is False
    assert fault.memory_mode(ws) == "warm"


def test_memory_cold_warm_lever_preserves_capture_and_inject_as(tmp_path):
    plan, _ = _memory_plan(tmp_path, "true")
    ws = _memory_working_spec(tmp_path)
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=1)
    spec = yaml.safe_load(ws.read_text())
    mem = spec["memory"]
    assert mem["enabled"] is True
    assert mem["inject_as"] == "_memory"            # untouched
    assert mem["capture"] == [{"stage": "researcher", "key": "content", "max_entries": 8}]
    assert mem["fresh"] is True                     # only fresh changed


def test_memory_cold_warm_lever_preserves_block_scalar_jinja_verbatim(tmp_path):
    """Regression: the lever must NOT yaml round-trip the spec, because safe_dump
    rewrites the block-scalar Jinja description into a double-quoted folded scalar
    with stray backslashes -> TemplateSyntaxError at run time. Edit only the
    `fresh:` line; every other line (including the multi-line Jinja description)
    must be byte-identical."""
    plan, _ = _memory_plan(tmp_path, "true")
    ws = _memory_working_spec(tmp_path)
    before = ws.read_text()
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=1)
    after = ws.read_text()
    # the only change is the `fresh:` line; reconstruct it by swapping that one line
    expected = before.replace("  fresh: false\n", "  fresh: true\n")
    assert after == expected, "lever must touch only the fresh: line, preserving the rest verbatim"
    # and specifically: no stray backslashes introduced into the description
    assert "\\" not in after, "stray backslash from yaml safe_dump folding would break Jinja"
    spec = yaml.safe_load(after)
    assert "{% if _memory is defined and _memory %}" in spec["stages"][0]["role"]["description"]
    assert "{{ _memory.researcher.content }}" in spec["stages"][0]["role"]["description"]


def test_memory_mode_none_when_no_memory_block(tmp_path):
    ws = tmp_path / "no_mem.yml"
    ws.write_text('name: wf\nversion: "1.0"\nstages: []\n')
    assert fault.memory_mode(ws) is None            # no memory block -> unlabeled


# ── memory_cold_warm lever, "alternate" mode (H4 v2 interleaving) ─────────

def _memory_plan_alt(tmp_path: Path) -> tuple:
    p = tmp_path / "plan.yml"
    p.write_text(textwrap.dedent("""
        name: t
        description: "x"
        workflow: s.yml
        budget: {max_runs: 9}
        phases:
          - id: p
            lever: memory_cold_warm
            inputs: {memory_fresh: "alternate"}
            repeats: 1
        verdicts: {}
    """))
    return load_plan(p), p


def test_memory_cold_warm_alternate_cold_on_even_warm_on_odd(tmp_path):
    """memory_fresh='alternate' interleaves cold/warm by rep parity so the H4
    verdict is not confounded by phase ordering: even rep = cold (fresh=true,
    ignore prior memory), odd rep = warm (fresh=false, inject it)."""
    plan, _ = _memory_plan_alt(tmp_path)
    ws = _memory_working_spec(tmp_path)
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=1)
    assert yaml.safe_load(ws.read_text())["memory"]["fresh"] is True
    assert fault.memory_mode(ws) == "cold"
    fault.apply_lever(plan.phases[0], phase_index=0, rep=1, corpus=[],
                      working_spec=ws, rng_seed=1)
    assert yaml.safe_load(ws.read_text())["memory"]["fresh"] is False
    assert fault.memory_mode(ws) == "warm"
    fault.apply_lever(plan.phases[0], phase_index=0, rep=2, corpus=[],
                      working_spec=ws, rng_seed=1)
    assert fault.memory_mode(ws) == "cold"          # alternates back


def test_memory_cold_warm_alternate_preserves_block_scalar_jinja(tmp_path):
    """The alternate mode must route through the same surgical fresh: edit —
    block-scalar Jinja stays verbatim, no stray backslashes (the round-trip
    hazard)."""
    plan, _ = _memory_plan_alt(tmp_path)
    ws = _memory_working_spec(tmp_path)
    before = ws.read_text()
    fault.apply_lever(plan.phases[0], phase_index=0, rep=0, corpus=[],
                      working_spec=ws, rng_seed=1)   # even -> fresh=true
    after = ws.read_text()
    expected = before.replace("  fresh: false\n", "  fresh: true\n")
    assert after == expected
    assert "\\" not in after