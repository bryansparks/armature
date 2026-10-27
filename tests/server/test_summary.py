"""A workflow in plain words: one sentence per step, and what changed between two versions."""
from __future__ import annotations

from armature.server.summary import describe_workflow, diff_summaries, readable

SPEC = """name: research
description: "Research {{ topic }} and write a briefing."
contracts:
  inputs:
    - name: topic
      required: true
    - name: focus
stages:
  - id: plan_searches
    role:
      name: Planner
      type: worker
      description: |
        Plan searches for {{ topic }}. {% if focus %}Focus on {{ focus | upper }}.{% endif %}
        Return a list of queries.
    output_mode: guided_json
    output_schema:
      type: object
      required: [queries]
      properties:
        queries: {type: array}
    depends_on: []
  - id: run_searches
    fan_out: 10
    partition_source: "{{ plan_searches.queries }}"
    partition_key: item
    tool_call:
      name: web_search
      args: {query: "{{ item.query }}"}
    depends_on: [plan_searches]
  - id: check
    adapter: demo/lint
    fail_as_value: true
    depends_on: [run_searches]
  - id: judge
    role: {name: Judge, type: judge, description: "Assess the results. Be strict."}
    on_fail:
      loop: {stage: judge, max: 2}
    depends_on: [check]
  - id: approve
    gate: human
    depends_on: [judge]
  - id: publish
    subagent_spec: publish.yml
    on_fail:
      loop: {stage: judge, max: 4}
    depends_on: [approve]
  - id: self_analyst
    post_run: true
    role: {name: Director, type: judge, description: "Review this run."}
"""


def _steps(summary):
    return {step["id"]: step for step in [*summary["steps"], *summary["post_run"]]}


def test_every_kind_of_step_reads_as_one_sentence():
    summary = describe_workflow(SPEC)
    assert summary["title"] == "research"
    assert summary["description"] == "Research ⟨topic⟩ and write a briefing."
    assert summary["inputs"] == [{"name": "topic", "required": True}, {"name": "focus"}]
    steps = _steps(summary)
    assert [s["id"] for s in summary["steps"]] == ["plan_searches", "run_searches", "check", "judge", "approve",
                                                   "publish"]
    assert [s["id"] for s in summary["post_run"]] == ["self_analyst"]
    plan = steps["plan_searches"]
    assert (plan["kind"], plan["label"]) == ("llm", "Planner")
    assert plan["what"] == "Planner: Plan searches for ⟨topic⟩."
    assert plan["output"] == "structured: queries" and plan["after"] == []
    search = steps["run_searches"]
    assert (search["kind"], search["what"]) == ("tool", "Calls tool web_search")
    assert search["fan_out"] == "for each item in ⟨plan_searches.queries⟩, up to 10 at a time"
    assert search["after"] == ["plan_searches"] and search["label"] == "Run searches"
    check = steps["check"]
    assert (check["kind"], check["what"], check["optional"]) == ("script", "Runs script demo/lint", True)
    assert steps["judge"]["retry"] == "Tries again up to 2 times if it fails"
    assert steps["judge"]["output"] == "text" and steps["judge"]["optional"] is False
    assert steps["judge"]["fan_out"] is None and plan["retry"] is None and summary["error"] is None
    assert (steps["approve"]["kind"], steps["approve"]["what"]) == ("gate", "Waits for human approval")
    assert (steps["publish"]["kind"], steps["publish"]["what"]) == ("subagent", "Starts workflow publish.yml")
    assert steps["publish"]["retry"] == "If it fails, goes back to judge, up to 4 times"
    assert steps["self_analyst"]["what"] == "Director: Review this run."


def test_jinja_reads_as_placeholders():
    assert readable("Hi {{ user.name | title }}{# note #}{% if x %}!{% endif %}") == "Hi ⟨user.name⟩!"
    assert readable(None) == ""


def test_the_same_body_always_reads_the_same():
    assert describe_workflow(SPEC) == describe_workflow(SPEC)


def test_a_body_that_is_not_a_workflow_says_so():
    for body in ("name: [unclosed", "- a list", "", None, 42):
        summary = describe_workflow(body)
        assert summary["steps"] == [] and summary["error"]


def test_a_stage_that_is_not_a_mapping_does_not_break_it():
    summary = describe_workflow("name: x\nstages:\n  - just text\n  - id: a\n    adapter: p/a\n")
    assert [s["id"] for s in summary["steps"]] == ["step-1", "a"]
    assert summary["steps"][0]["kind"] == "other"


def test_diff_names_added_removed_and_changed_steps():
    old = describe_workflow(SPEC)
    changed_spec = (SPEC.replace("fan_out: 10", "fan_out: 3")
                    .replace("  - id: approve\n    gate: human\n    depends_on: [judge]\n", "")
                    .replace("depends_on: [approve]", "depends_on: [judge]")
                    .replace("Be strict.", "Be lenient.")
                    + "  - id: notify\n    adapter: chat/post\n    depends_on: [publish]\n")
    changes = diff_summaries(old, describe_workflow(changed_spec))
    assert changes["added"] == ["notify"]
    assert changes["removed"] == ["approve"]
    by_id = {entry["id"]: entry["fields"] for entry in changes["changed"]}
    assert by_id["run_searches"] == ["fan_out"]
    assert by_id["publish"] == ["after"]
    assert by_id["judge"] == ["details"]
    assert changes["renamed"] == [] and changes["workflow"] == []


def test_diff_spots_a_renamed_step_and_workflow_fields():
    old = describe_workflow(SPEC)
    renamed = SPEC.replace("id: approve", "id: sign_off").replace("[approve]", "[sign_off]")
    renamed = renamed.replace("name: research", "name: research-v2").replace("    - name: focus\n", "")
    changes = diff_summaries(old, describe_workflow(renamed))
    assert changes["renamed"] == [{"from": "approve", "to": "sign_off"}]
    assert changes["added"] == [] and changes["removed"] == []
    assert {"id": "publish", "fields": ["after"]} in changes["changed"]
    assert changes["workflow"] == ["title", "inputs"]


def test_diff_from_nothing_adds_every_step():
    changes = diff_summaries(describe_workflow(""), describe_workflow(SPEC))
    assert changes["added"] == ["plan_searches", "run_searches", "check", "judge", "approve", "publish",
                                "self_analyst"]
    assert changes["removed"] == [] and changes["changed"] == []
