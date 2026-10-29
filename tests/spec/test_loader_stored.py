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
