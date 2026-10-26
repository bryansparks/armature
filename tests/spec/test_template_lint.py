"""The template lint keeps a stored workflow's templates to a safe subset."""
import pytest
from typer.testing import CliRunner

from armature.cli import app
from armature.spec.models import (
    Adapter, HarnessSpec, IterationConfig, LoopConfig, OnFailConfig, Role, RoleType, Stage, ToolCallConfig,
)
from armature.spec.template_lint import lint_spec, lint_template, lint_tree

# Expressions the armature-ops specs use today, inlined so the test reads nothing outside this repo.
OPS_TEMPLATES = [
    "{{ classify.intent == 'ignore' or classify.confidence < 0.7 }}",
    "{{ classify.intent != 'create' or classify.confidence < 0.7 }}",
    "{{ (prepare.platform | default('')) != '' }}",
    "{{ assignments.jobs }}",
    "{{ trigger_payload.text }}",
    "Message: {{ trigger_payload.text }}\nIntent: {{ classify.intent }}",
    "$OPS_HOME/scripts/pr_resolve.sh --slug-from '.trigger_payload.slug'",
]


@pytest.mark.parametrize("text", OPS_TEMPLATES)
def test_accepts_ops_templates(text):
    assert lint_template(text) == []


@pytest.mark.parametrize("text", [
    "{{ x['a'][0] }}",
    "{{ a if b else c }}",
    "{{ not x and y in [1, 2] }}",
    "{{ {'a': 1} | tojson }}",
    "{{ items | length > 0 }}",
    "{{ x | default('', true) | trim | lower }}",
])
def test_accepts_safe_subset(text):
    assert lint_template(text) == []


@pytest.mark.parametrize("text, needle", [
    ("{{ lipsum() }}", "calls"),
    ("{{ x.items() }}", "calls"),
    ("{% for a in b %}{{ a }}{% endfor %}", "{% for %}"),
    ("{% set a = 1 %}", "{% assign %}"),
    ("{{ ''.__class__ }}", "'__class__'"),
    ("{{ _private }}", "'_private'"),
    ("{{ x._secret }}", "'_secret'"),
    ("{{ x['__globals__'] }}", "'__globals__'"),
    ("{{ x[y] }}", "constant"),
    ("{{ x | attr('y') }}", "filter 'attr'"),
    ("{{ x is defined }}", "tests"),
    ("{{ a ~ b }}", "Concat"),
    ("{{ unclosed", "syntax error"),
])
def test_rejects_unsafe(text, needle):
    errors = lint_template(text)
    assert errors
    assert any(needle in e for e in errors)


def _spec(**stage_fields) -> HarnessSpec:
    return HarnessSpec(
        name="lint",
        roles={"r": Role(name="r", type=RoleType.WORKER, description="Hi {{ x.__class__ }}")},
        adapters={"a": Adapter(name="a", type="script", cmd="echo {{ x() }}")},
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[], **stage_fields)],
    )


def test_lint_spec_covers_every_template_field():
    spec = _spec(
        skip_if="{{ x() }}",
        condition="{{ x() }}",
        partition_source="{{ x() }}",
        present="{{ x() }}",
        loop=IterationConfig(until="{{ x() }}"),
        on_fail=OnFailConfig(loop=LoopConfig(stage="s", until="{{ x() }}")),
    )
    spec.stages[0].tool_call.args = {"nested": {"v": "{{ x() }}"}}
    fields = {(e.stage_id, e.message.split(":")[0]) for e in lint_spec(spec)}
    assert fields == {
        (None, "roles.r.description"),
        (None, "adapters.a.cmd"),
        ("s", "stages[0].skip_if"),
        ("s", "stages[0].condition"),
        ("s", "stages[0].partition_source"),
        ("s", "stages[0].present"),
        ("s", "stages[0].loop.until"),
        ("s", "stages[0].on_fail.loop.until"),
        ("s", "stages[0].tool_call.args.nested.v"),
    }
    assert all(e.code == "UNSAFE_TEMPLATE" for e in lint_spec(spec))


def test_lint_tree_walks_every_string_scalar():
    data = {
        "name": "{{ x() }}",
        "description": "plain text",
        "context_layers": [{"name": "l", "content": "{{ cfg.__class__ }}"}],
        "stages": [{"id": "s", "tool_call": {"name": "t"}, "notes": ["ok", "{% for a in b %}{% endfor %}"]}],
    }
    errors = {(e.stage_id, e.message.split(":")[0]) for e in lint_tree(data)}
    assert errors == {
        (None, "name"),
        (None, "context_layers[0].content"),
        ("s", "stages[0].notes[1]"),
    }


def test_rejects_raw_block_hiding_a_template():
    errors = lint_template("{% raw %}{{ x.__class__ }}{% endraw %}")
    assert any("{% raw %}" in e for e in errors)


def test_allows_result_but_no_other_private_name():
    assert lint_template("{{ _result == 'done' }}") == []
    assert lint_template("{{ _results }}")
    assert lint_template("{{ x._result }}")


def test_lint_spec_accepts_ops_style_spec():
    spec = HarnessSpec(
        name="ok",
        stages=[
            Stage(id="s", tool_call=ToolCallConfig(name="t", args={"text": "{{ trigger_payload.text }}"}),
                  skip_if="{{ classify.intent == 'ignore' or classify.confidence < 0.7 }}",
                  partition_source="{{ assignments.jobs }}", fan_out=3, depends_on=[]),
        ],
    )
    assert lint_spec(spec) == []


def _write(tmp_path, skip_if):
    spec = tmp_path / "s.yml"
    spec.write_text(
        "name: s\n"
        "stages:\n"
        "  - id: s\n"
        "    tool_call: {name: t}\n"
        f"    skip_if: \"{skip_if}\"\n"
        "    depends_on: []\n"
    )
    return spec


def test_validate_strict_templates_flag(tmp_path):
    spec = _write(tmp_path, "{{ x | attr('y') }}")
    runner = CliRunner()
    assert runner.invoke(app, ["validate", str(spec)]).exit_code == 0
    strict = runner.invoke(app, ["validate", str(spec), "--strict-templates"])
    assert strict.exit_code == 1
    assert "UNSAFE_TEMPLATE" in strict.output


def test_validate_strict_templates_lints_load_time_strings(tmp_path):
    spec = tmp_path / "s.yml"
    spec.write_text(
        "name: s\n"
        "description: \"{{ topic.__class__ }}\"\n"
        "stages:\n"
        "  - id: s\n"
        "    tool_call: {name: t}\n"
        "    depends_on: []\n"
    )
    strict = CliRunner().invoke(app, ["validate", str(spec), "--strict-templates"])
    assert strict.exit_code == 1
    assert "description" in strict.output


def test_validate_strict_templates_passes_safe_spec(tmp_path):
    spec = _write(tmp_path, "{{ x.y == 1 }}")
    assert CliRunner().invoke(app, ["validate", str(spec), "--strict-templates"]).exit_code == 0
