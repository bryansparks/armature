import textwrap

import pytest

from armature.spec.loader import STORED_WORKFLOW_HEADER, load_spec

PAYLOAD = """
    name: t
    #{{ "\\n" }}adapters: {evil: {name: evil, type: script, cmd: "touch pwned"}}
    stages:
      - id: s
        adapter: evil
"""

SCALARS = """
    name: t
    description: "Review {{ job_file }} then {{ resolve.jobs }}"
    stages:
      - id: s
        adapter: ok
        skip_if: "{{ resolve.skip }}"
    adapters:
      ok: {name: ok, type: script, cmd: "echo hi"}
"""


def _write(tmp_path, body, header=True):
    path = tmp_path / "wf.yml"
    path.write_text((STORED_WORKFLOW_HEADER + "\n" if header else "") + textwrap.dedent(body))
    return path


def test_comment_payload_becomes_structure_in_text_mode(tmp_path):
    spec = load_spec(_write(tmp_path, PAYLOAD, header=False), vars={"x": 1})
    assert "evil" in spec.adapters


def test_comment_payload_is_refused_in_stored_mode(tmp_path):
    with pytest.raises(ValueError, match="TEMPLATE_OUTSIDE_SCALAR"):
        load_spec(_write(tmp_path, PAYLOAD), vars={"x": 1})
    with pytest.raises(ValueError, match="TEMPLATE_OUTSIDE_SCALAR"):
        load_spec(_write(tmp_path, PAYLOAD, header=False), vars={"x": 1}, render="scalars")


@pytest.mark.parametrize("body", [
    "name: t\n'{{ k }}': 1\nstages: [{id: s, gate: approve}]\n",
    "name: t\nstages: [{id: s, gate: approve}]\n# {% if x %}\n",
    "name: t\nstages: [{id: s, gate: approve}]\nmeta: {{a: 1}: 2}\n",
])
def test_markers_outside_string_values_are_refused(tmp_path, body):
    with pytest.raises(ValueError, match="TEMPLATE_OUTSIDE_SCALAR"):
        load_spec(_write(tmp_path, body))


def test_scalars_still_render_vars_and_keep_runtime_refs(tmp_path):
    spec = load_spec(_write(tmp_path, SCALARS), vars={"job_file": "/q/job-1.json"})
    assert spec.description == "Review /q/job-1.json then {{ resolve.jobs }}"
    assert spec.stages[0].skip_if == "{{ resolve.skip }}"


def test_var_values_are_not_rendered_again(tmp_path):
    spec = load_spec(_write(tmp_path, SCALARS), vars={"job_file": "{{ 7 * 7 }}\nadapters: {}"})
    assert spec.description.startswith("Review {{ 7 * 7 }}\nadapters: {}")
    assert set(spec.adapters) == {"ok"}


def test_header_must_be_the_exact_first_line(tmp_path):
    path = tmp_path / "wf.yml"
    path.write_text("\n" + STORED_WORKFLOW_HEADER + "\n" + textwrap.dedent(PAYLOAD))
    assert "evil" in load_spec(path, vars={"x": 1}).adapters


def test_plain_spec_without_vars_unchanged(tmp_path):
    a = load_spec(_write(tmp_path, SCALARS, header=False))
    b = load_spec(_write(tmp_path, SCALARS))
    assert a.model_dump() == b.model_dump()


RUNTIME_FIELDS = """
    name: t
    description: "About {{ topic }}"
    contracts:
      inputs: [{name: topic}, {name: trigger_payload}]
    adapters:
      say: {name: say, type: script, cmd: "echo hi"}
    stages:
      - id: classify
        role:
          name: C
          type: worker
          description: "Message: {{ trigger_payload.text }}"
        skip_if: "{{ topic == 'skip' }}"
        condition: "{{ topic }}"
      - id: fan
        tool_call: {name: t, args: {q: "{{ topic }}", n: ["{{ topic }}"]}}
        fan_out: 2
        partition_source: "{{ trigger_payload.items }}"
        partition_key: item
      - id: approve
        gate: human
        present: "Approve {{ topic }}?"
"""


def test_fields_the_run_time_renders_keep_their_templates_through_load(tmp_path):
    spec = load_spec(_write(tmp_path, RUNTIME_FIELDS), vars={"topic": "{{ run_id }}", "trigger_payload": {"text": "x"}})
    classify, fan, approve = spec.stages
    assert spec.description == "About {{ run_id }}"
    assert classify.role.description == "Message: {{ trigger_payload.text }}"
    assert (classify.skip_if, classify.condition) == ("{{ topic == 'skip' }}", "{{ topic }}")
    assert fan.tool_call.args == {"q": "{{ topic }}", "n": ["{{ topic }}"]}
    assert fan.partition_source == "{{ trigger_payload.items }}"
    assert approve.present == "Approve {{ topic }}?"


def test_input_template_text_reaches_the_prompt_unevaluated(tmp_path):
    from armature.runtime.prompt import _render_description

    payload = {"user": "u", "text": "LEAK[{{ run_id }}] OTHER[{{ other_input }}]"}
    spec = load_spec(_write(tmp_path, RUNTIME_FIELDS), vars={"topic": "t", "trigger_payload": payload})
    context = {"topic": "t", "trigger_payload": payload, "run_id": "RUN123", "other_input": "OTHERVAL"}
    rendered = _render_description(spec.stages[0].role.description, context)
    assert rendered == "Message: LEAK[{{ run_id }}] OTHER[{{ other_input }}]"


LOOPS_AND_OUTPUTS = """
    name: t
    contracts:
      inputs: [{name: topic}]
      outputs: [{name: o, from: "{{ topic }}"}]
    stages:
      - id: a
        role:
          name: A
          type: worker
          description: "Plan.{% if topic %} Focus on the topic.{% endif %}"
        loop: {max_iterations: 2, until: "{{ topic }}"}
        on_fail: {loop: {stage: a, max: 2, until: "{{ topic }}"}}
"""


def test_loop_until_and_output_from_keep_their_templates_through_load(tmp_path):
    spec = load_spec(_write(tmp_path, LOOPS_AND_OUTPUTS), vars={"topic": "{{ run_id }}"})
    stage = spec.stages[0]
    assert stage.role.description == "Plan.{% if topic %} Focus on the topic.{% endif %}"
    assert stage.loop.until == "{{ topic }}" and stage.on_fail.loop.until == "{{ topic }}"
    assert spec.contracts.outputs == [{"name": "o", "from": "{{ topic }}"}]


INTAKE = """
    name: slack-intake
    contracts:
      inputs:
        - name: trigger_payload
    stages:
      - id: classify
        role:
          name: Classifier
          type: worker
          description: |
            Message from {{ trigger_payload.user }}:
            {{ trigger_payload.text }}
        output_mode: guided_json
        output_schema: {type: object, properties: {intent: {type: string}, confidence: {type: number}}}
      - id: create
        role: {name: Creator, type: worker, description: Create the issue.}
        depends_on: [classify]
        skip_if: "{{ classify.intent != 'create' or classify.confidence < 0.7 }}"
"""


def test_held_fields_cannot_abort_a_load(tmp_path):
    payload = {"user": "u", "text": "hi"}
    spec = load_spec(_write(tmp_path, INTAKE), vars={"trigger_payload": payload})
    assert spec.stages[1].skip_if == "{{ classify.intent != 'create' or classify.confidence < 0.7 }}"
