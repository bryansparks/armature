import pytest
from pydantic import ValidationError

from armature.spec.loader import load_spec
from armature.spec.models import AgentStep, HarnessSpec, Stage
from armature.spec.validator import validate_spec


def codes(spec, **kw):
    return {(e.code, e.severity) for e in validate_spec(spec, strict=False, **kw)}


def one(agent, **stage):
    return HarnessSpec(name="wf", stages=[Stage(id="a", agent={"model": "sonnet", "prompt": "p", **agent}, **stage)])


def test_mapping_parses_and_string_still_names_a_library_agent():
    stage = Stage(id="a", agent={"model": "sonnet", "prompt": "Review."})
    assert isinstance(stage.agent, AgentStep)
    assert stage.agent.access == "read" and stage.agent.harness == "claude-code"
    assert Stage(id="b", agent="reviewer").agent == "reviewer"


@pytest.mark.parametrize("bad", [{"hooks": True}, {"access": "admin"}, {"effort": "huge"}, {"harness": "gpt"},
                                 {"unknown": 1}])
def test_bad_fields_are_schema_errors(bad):
    with pytest.raises(ValidationError):
        Stage(id="a", agent={"model": "m", "prompt": "p", **bad})


def test_a_valid_agent_stage_has_no_errors():
    assert codes(one({"skills": ["kotlin-review"], "context_files": {"pr_body": "{{ body }}"}})) == set()


@pytest.mark.parametrize("agent", [{"model": "{{ m }}"}, {"skills": ["{{ s }}"]}, {"allowed_tools": ["{% x %}"]},
                                   {"disallowed_tools": ["{# c #}"]}])
def test_templates_outside_prompt_and_context_files_are_rejected(agent):
    assert ("AGENT_TEMPLATE", "error") in codes(one(agent))


def test_read_access_keeps_read_tools():
    assert ("AGENT_READ_TOOLS", "error") in codes(one({"allowed_tools": ["Read", "Bash"]}))
    assert ("AGENT_READ_TOOLS", "error") not in codes(one({"access": "write", "allowed_tools": ["Read", "Bash"]}))


def test_skill_names_and_unknown_skills():
    assert ("AGENT_SKILL_NAME", "error") in codes(one({"skills": ["../etc"]}))
    assert ("AGENT_UNKNOWN_SKILL", "error") in codes(one({"skills": ["nope"]}), skill_names=["other"])
    assert ("AGENT_UNKNOWN_SKILL", "error") not in codes(one({"skills": ["nope"]}))


def test_repo_skills_and_rules():
    assert codes(one({"repo_skills": ["nephi-bot"], "repo_rules": ["docs/rules/**"]})) == set()
    assert codes(one({"repo_skills": True})) == set()
    assert ("AGENT_SKILL_NAME", "error") in codes(one({"repo_skills": ["../etc"]}))
    assert ("AGENT_TEMPLATE", "error") in codes(one({"repo_skills": ["{{ s }}"]}))
    assert ("AGENT_TEMPLATE", "error") in codes(one({"repo_rules": ["{{ r }}"]}))
    assert ("AGENT_SKILL_SHADOWED", "warning") in codes(one({"skills": ["a"], "repo_skills": ["a"]}))
    for rule in ("/etc/x", "../x", "docs/../../x", "~/x", ""):
        assert ("AGENT_REPO_RULE", "error") in codes(one({"repo_rules": [rule]}))
    assert Stage(id="a", agent={"model": "m", "prompt": "p"}).agent.repo_skills is None


def test_context_file_names_and_unsafe_templates():
    assert ("AGENT_CONTEXT_FILE", "error") in codes(one({"context_files": {"../x": "t"}}))
    assert ("UNSAFE_TEMPLATE", "error") in codes(one({"prompt": "{{ x.__class__ }}"}))
    assert ("UNSAFE_TEMPLATE", "error") in codes(one({"context_files": {"c": "{{ x.__class__ }}"}}))


def test_guided_json_needs_a_schema():
    assert ("AGENT_SCHEMA_MISSING", "error") in codes(one({}, output_mode="guided_json"))


def test_conflicts_warnings_and_timeout():
    assert ("AGENT_CONFLICTING_EXECUTION", "error") in codes(one({}, adapter="x"))
    spec = one({"access": "write"}, fan_out=2, partition_source="{{ xs }}")
    assert ("AGENT_WRITE_FAN_OUT", "warning") in codes(spec)
    assert ("AGENT_EXPERIMENTAL", "warning") in codes(one({"harness": "codex"}))
    assert ("AGENT_TIMEOUT", "error") in codes(one({"timeout_s": 0}))


def test_agent_stage_counts_as_an_execution_type():
    assert "NO_EXECUTION_TYPE" not in {c for c, _ in codes(one({}))}


STORED = """# armature: stored-workflow
name: wf
contracts:
  inputs:
    - name: body
stages:
  - id: review
    depends_on: []
    agent:
      model: sonnet
      prompt: "Title {{ body }}"
      context_files:
        pr_body: "{{ body }}"
"""


@pytest.mark.parametrize("header", [True, False])
def test_inputs_never_render_into_an_agent_at_load(tmp_path, header):
    path = tmp_path / "wf.yaml"
    path.write_text(STORED if header else STORED.split("\n", 1)[1])
    spec = load_spec(path, {"body": "{{ 7 * 7 }}"})
    agent = spec.stages[0].agent
    assert agent.prompt == "Title {{ body }}"
    assert agent.context_files == {"pr_body": "{{ body }}"}
