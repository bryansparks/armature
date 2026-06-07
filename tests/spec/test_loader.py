import pytest
from pathlib import Path
from armature.spec.loader import load_spec

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_load_minimal_spec():
    spec = load_spec(FIXTURES / "minimal.yaml")
    assert spec.name == "minimal-workflow"
    assert len(spec.stages) == 1
    assert spec.stages[0].id == "step1"


def test_load_with_template_vars():
    spec = load_spec(FIXTURES / "minimal.yaml", vars={"run_id": "abc123"})
    assert spec.name == "minimal-workflow"


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_spec(Path("/nonexistent/spec.yaml"))


def test_load_with_none_vars_same_as_no_vars():
    spec_none = load_spec(FIXTURES / "minimal.yaml", vars=None)
    spec_no = load_spec(FIXTURES / "minimal.yaml")
    assert spec_none.name == spec_no.name


def test_load_echo_workflow():
    spec = load_spec(FIXTURES / "echo-workflow.yaml")
    assert spec.name == "echo-workflow"
    assert len(spec.stages) == 2
    assert spec.adapters["echo_message"].cmd is not None


def test_load_template_vars_render_into_spec_structure(tmp_path):
    """Template vars substitute into spec-level fields like name."""
    spec_file = tmp_path / "templated.yaml"
    spec_file.write_text(
        'name: "wf-{{ suffix }}"\n'
        'stages:\n'
        '  - id: s1\n'
        '    tool_call:\n'
        '      name: t\n'
        '    depends_on: []\n'
    )
    spec = load_spec(spec_file, vars={"suffix": "test123"})
    assert spec.name == "wf-test123"


def test_load_bad_yaml_raises(tmp_path):
    """Malformed YAML raises a parse error."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [\n  unclosed\n")
    with pytest.raises(Exception):
        load_spec(bad)


def test_load_invalid_model_raises(tmp_path):
    """Valid YAML that fails Pydantic validation raises."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [1, 2, 3]\nstages: []\n")  # name must be a string
    with pytest.raises(Exception):
        load_spec(bad)


def test_load_returns_harness_spec_with_model_tiers(tmp_path):
    """Spec with model_tiers section populates ModelTiers correctly."""
    spec_file = tmp_path / "tiers.yaml"
    spec_file.write_text(
        'name: wf\n'
        'model_tiers:\n'
        '  small:\n'
        '    provider: openai\n'
        '    model: gpt-4o-mini\n'
        'stages:\n'
        '  - id: s1\n'
        '    tool_call:\n'
        '      name: t\n'
        '    depends_on: []\n'
    )
    spec = load_spec(spec_file)
    assert spec.model_tiers.small is not None
    assert spec.model_tiers.small.model == "gpt-4o-mini"


def test_load_spec_as_string_path(tmp_path):
    """load_spec accepts str path in addition to Path objects."""
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(
        'name: str-path-test\n'
        'stages:\n'
        '  - id: s\n'
        '    tool_call:\n'
        '      name: t\n'
        '    depends_on: []\n'
    )
    spec = load_spec(str(spec_file))
    assert spec.name == "str-path-test"


def test_vars_do_not_destroy_runtime_jinja2_expressions(tmp_path):
    """Runtime Jinja2 expressions like {{ upstream.key }} must be preserved
    when load_spec is called with vars containing only user inputs.

    This is the root cause of the partition_source NoneType bug:
    load_spec rendered '{{ plan_searches.queries }}' to '' (ChainableUndefined)
    then NativeEnvironment.from_string('').render() returned None.
    """
    spec_file = tmp_path / "fanout.yaml"
    spec_file.write_text(
        'name: test-fanout\n'
        'stages:\n'
        '  - id: planner\n'
        '    role:\n'
        '      name: Planner\n'
        '      type: worker\n'
        '      description: "Plan for {{ topic }}"\n'
        '    output_mode: guided_json\n'
        '    output_schema:\n'
        '      type: object\n'
        '      properties:\n'
        '        items: {type: array}\n'
        '    depends_on: []\n'
        '  - id: worker\n'
        '    fan_out: 5\n'
        '    fan_in: list\n'
        '    partition_source: "{{ planner.items }}"\n'
        '    partition_key: item\n'
        '    tool_call:\n'
        '      name: my_tool\n'
        '      args:\n'
        '        input: "{{ item.value }}"\n'
        '    depends_on: [planner]\n'
    )
    # Simulate what the CLI does: pass user inputs as vars
    spec = load_spec(spec_file, vars={"topic": "AI research"})

    worker = next(s for s in spec.stages if s.id == "worker")
    # partition_source must be preserved as a Jinja2 expression, not erased
    assert worker.partition_source == "{{ planner.items }}", (
        f"partition_source was destroyed by spec-load rendering: {worker.partition_source!r}"
    )
    # tool_call args must also be preserved
    assert worker.tool_call.args["input"] == "{{ item.value }}", (
        f"tool_call arg was destroyed: {worker.tool_call.args['input']!r}"
    )
    # spec-level vars ARE substituted (correct behavior)
    planner = next(s for s in spec.stages if s.id == "planner")
    assert "AI research" in planner.role.description


def test_vars_substituted_in_spec_level_fields(tmp_path):
    """Spec-level vars ARE substituted into spec fields like name."""
    spec_file = tmp_path / "templated.yaml"
    spec_file.write_text(
        'name: "wf-{{ env }}"\n'
        'stages:\n'
        '  - id: s1\n'
        '    tool_call:\n'
        '      name: t\n'
        '    depends_on: []\n'
    )
    spec = load_spec(spec_file, vars={"env": "staging"})
    assert spec.name == "wf-staging"
