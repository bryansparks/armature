"""Every template Armature renders goes through a sandbox."""
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from jinja2 import ChainableUndefined, UndefinedError
from jinja2.sandbox import SecurityError

from armature.nodes.gate import HumanGateNode
from armature.nodes.script import ScriptNode
from armature.nodes.tool_call import _render_args
from armature.permissions.permissions import PermissionLevel
from armature.registry.registry import ToolDescriptor
from armature.runtime.engine import Harness
from armature.runtime.prompt import _render_description
from armature.runtime import templating
from armature.runtime.templating import (
    MAX_OUTPUT_CHARS, SandboxedNativeEnvironment, TemplateLimitError, native_env, text_env,
)
from armature.spec.loader import load_spec
from armature.spec.models import (
    Adapter, HarnessSpec, ModelTierConfig, ModelTiers, Stage, ToolCallConfig,
)

SSTI = "{{ ''.__class__.__mro__[1].__subclasses__() }}"


def test_native_env_keeps_native_types():
    env = native_env(ChainableUndefined)
    result = env.from_string("{{ assignments.jobs }}").render(assignments={"jobs": [{"a": 1}, {"b": 2}]})
    assert result == [{"a": 1}, {"b": 2}]


def test_native_env_is_sandboxed():
    env = native_env(ChainableUndefined)
    assert isinstance(env, SandboxedNativeEnvironment)
    assert env.sandboxed
    with pytest.raises(SecurityError):
        env.from_string(SSTI).render()


def test_text_env_is_immutable_sandbox():
    with pytest.raises(SecurityError):
        text_env().from_string("{{ items.append(1) }}").render(items=[])


def test_environments_are_cached():
    assert text_env(ChainableUndefined) is text_env(ChainableUndefined)
    assert native_env(ChainableUndefined) is native_env(ChainableUndefined)


def _harness(stage: Stage, tmp_path: Path, ran: list) -> Harness:
    async def tool(args):
        ran.append(args)
        return {"ok": True}

    spec = HarnessSpec(
        name="ssti",
        stages=[stage],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._registry.register(ToolDescriptor(
        name="t", description="t", permission=PermissionLevel.READ_ONLY, handler=tool, parameters={},
    ))
    return harness


@pytest.mark.parametrize("field", ["skip_if", "condition", "partition_source"])
async def test_engine_refuses_ssti(field, tmp_path):
    extra = {"fan_out": 2} if field == "partition_source" else {}
    stage = Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[], **{field: SSTI}, **extra)
    ran: list = []
    with pytest.raises(Exception, match="unsafe"):
        await _harness(stage, tmp_path, ran).run({})
    assert ran == []


def test_until_refuses_ssti():
    with pytest.raises(SecurityError):
        Harness._eval_until(SSTI, {}, {})


async def test_gate_message_refuses_ssti():
    node = HumanGateNode(stage=Stage(id="g", gate="human", present=SSTI))
    with patch("builtins.input", return_value="yes"), pytest.raises(SecurityError):
        await node.execute({})


def test_tool_args_refuse_ssti():
    with pytest.raises(SecurityError):
        _render_args({"x": SSTI}, {})


def test_tool_args_still_return_lists():
    assert _render_args({"x": "{{ assignments.jobs }}"}, {"assignments": {"jobs": [1, 2]}}) == {"x": [1, 2]}


def test_prompt_description_renders_inert():
    assert _render_description(SSTI, {}) == SSTI


async def test_script_cmd_refuses_ssti():
    node = ScriptNode(adapter=Adapter(name="a", type="script", cmd="echo " + SSTI))
    with pytest.raises(SecurityError):
        await node.execute({})


def test_load_spec_vars_refuse_ssti(tmp_path):
    spec = tmp_path / "s.yml"
    spec.write_text("name: " + SSTI + "\nstages: []\n")
    with pytest.raises((SecurityError, UndefinedError)):
        load_spec(spec, vars={"topic": "x"})


def test_load_spec_vars_hide_attributes(tmp_path):
    spec = tmp_path / "s.yml"
    spec.write_text("name: \"{{ topic.__class__ }}\"\nstages: []\n")
    assert "<class" not in load_spec(spec, vars={"topic": "x"}).name


def test_load_spec_keeps_runtime_placeholders(tmp_path):
    spec = tmp_path / "s.yml"
    spec.write_text(
        "name: '{{ topic }}'\n"
        "stages:\n"
        "  - id: s\n"
        "    tool_call: {name: t}\n"
        "    partition_source: '{{ planner.items }}'\n"
        "    depends_on: []\n"
    )
    loaded = load_spec(spec, vars={"topic": "demo"})
    assert loaded.name == "demo"
    assert loaded.stages[0].partition_source == "{{ planner.items }}"


HUGE = [
    "{{ 'x' * 50000000 }}", "{{ 50000000 * 'x' }}", "{{ [1] * 50000000 }}", "{{ 2 ** 10000000 }}",
    "{% set a = 'x' * 100000 %}{{ a ~ a ~ a ~ a ~ a ~ a ~ a ~ a ~ a ~ a ~ a }}",
    "{% set a = 'x' * 100000 %}{{ a + a + a + a + a + a + a + a + a + a + a }}",
    "{% set a = ['x'] * 100000 %}{{ (a + a + a + a + a + a + a + a + a + a + a) | length }}",
    "{{ '%99999999s' % 'x' }}", "{{ '%.99999999f' % 1.0 }}", "{{ '%*s' % (99999999, 'x') }}",
    "{{ '{:99999999}'.format('x') }}", "{{ '{:{w}}'.format('x', w=99999999) }}",
    "{{ 'x' | center(99999999) }}", "{{ 'x' | indent(99999999) }}", "{{ ('a\n' * 50000) | indent(50000) }}",
    "{{ 'x' | wordwrap(99999999) }}", "{{ 'x' | truncate(99999999) }}", "{{ '%99999999s' | format('x') }}",
    "{{ ('a' * 99999) | replace('a', 'b' * 99999) }}", "{{ ('a' * 99999).replace('a', 'b' * 99999) }}",
    "{{ ['b' * 99999, 'b' * 99999, 'b' * 99999, 'b' * 99999, 'b' * 99999, 'b' * 99999, 'b' * 99999, 'b' * 99999,"
    " 'b' * 99999, 'b' * 99999, 'b' * 99999] | join }}",
    "{{ ('b' * 99999).join(['c'] * 99999) }}",
    "{{ 'x'.ljust(99999999) }}", "{{ 'x'.zfill(99999999) }}", "{{ ('\t' * 99999).expandtabs(99999) }}",
    "{% for a in range(100000) %}{% for b in range(100000) %}{% endfor %}{% endfor %}",
    "{% macro m() %}{% endmacro %}{% for a in range(100000) %}{% for b in range(100000) %}{{ m() }}"
    "{% endfor %}{% endfor %}",
    "{% for a in range(20) %}{{ 'x' * 99999 }}{% endfor %}",
    "{{ ['x' * 99999] * 99999 }}", "{{ ['x' * 99999] * 50 }}",
    "{% set l = ['x' * 99999] * 9 %}{% for i in range(99999) %}{% set s = l | string %}{% endfor %}",
    "{% set l = ['x' * 99999] * 9 %}{% for i in range(99999) %}{% set s = l | tojson %}{% endfor %}",
    "{% set l = ['x' * 99999] * 9 %}{% for i in range(99999) %}{% set s = l | pprint %}{% endfor %}",
    "{% set l = ['x' * 99999] * 9 %}{% for i in range(99999) %}{% set s = l ~ '' %}{% endfor %}",
    "{{ ['a' * 99999] | map('replace', 'a', 'bb' * 50) | list | length }}",
    "{% set l = ['ab' * 49999] * 9 %}{% for i in range(9999) %}{% set s = l | map('center', 99999) | list %}"
    "{% endfor %}",
    "{{ ['x'] | sum(start='y' * 99999) }}", "{{ [['x' * 99999]] | sum(start=[]) }}",
    "{{ 'abc'.translate({97: 'x' * 99999}) }}",
    "{% for i in range(99999) %}{% set x = ('x' * 99999).upper() %}{% endfor %}",
    "{% for i in range(99999) %}{% set x = 'x' * 99999 %}{% endfor %}",
    "{{ '{0}{0}{0}{0}{0}{0}{0}{0}{0}{0}{0}{0}'.format('x' * 99999) }}",
    "{% set x = 'x' * 99999 %}{{ '%s%s%s%s%s%s%s%s%s%s%s%s' % (x, x, x, x, x, x, x, x, x, x, x, x) }}",
    "{% for i in range(99999) %}{% for j in range(99999) %}{% endfor %}{% endfor %}",
    "{% for i in range(99999) %}{% set s = 'x'*99999 %}{% endfor %}",
    "{% for i in range(999) %}{% for j in range(999) %}{{ ('x'*99999)|upper }}{% endfor %}{% endfor %}",
    "{% set ns = namespace(l=[]) %}{% for i in range(99999) %}{% set ns.l = ns.l + ['x'*99999] %}{% endfor %}",
    "{{ ('x'*99999).translate({120: 'y'*99999}) }}",
    "{{ ([[0]*99999]*300)|sum(start=[]) }}",
    "{{ (['x'*99999]*2000)|string }}",
    "{{ (['x'*99999]*2000)|tojson }}",
    "{{ (['x'*99999]*2000)|pprint }}",
    "{{ range(2000)|map('center', 99999)|list }}",
    "{{ range(99999)|map('string')|map('center', 99999)|join }}",
    "{{ ('x\n'*50000)|wordwrap(1) }}",
    "{% set l = ['x'*99999] %}{{ (l * 20) | length }}",
    "{% set a = ['x'*99999]*6 %}{{ (a + a) | length }}",
    "{% set a = range(60000) | list %}{{ (a + a) | length }}",
    "{% set l = ['x'*99999]*9 %}{% for i in range(9999) %}{% set n = l | length %}{% endfor %}",
    "{% set l = ['x'*99999]*9 %}{% for i in range(9999) %}{% set n = l | first %}{% endfor %}",
    "{% set d = {'a': 1} %}{% for i in range(60000) %}{% set x = d.a %}{% set y = d.a %}{% endfor %}",
    "{% set d = {'a': 1} %}{% for i in range(60000) %}{% set x = d['a'] %}{% set y = d['a'] %}{% endfor %}",
    "{{ range(99999) | select('odd') | map('abs') | list | length }}",
]


@pytest.mark.parametrize("source", HUGE)
@pytest.mark.parametrize("env", [text_env(ChainableUndefined), native_env()], ids=["text", "native"])
def test_huge_templates_fail_fast(env, source):
    started = time.monotonic()
    with pytest.raises(TemplateLimitError):
        env.from_string(source).render()
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("env", [text_env(), native_env()], ids=["text", "native"])
def test_nested_loops_fail_in_under_a_second(env):
    loops = "{% for a in range(99999) %}{% for b in range(99999) %}{% for c in range(99999) %}" \
            "{% endfor %}{% endfor %}{% endfor %}"
    started = time.monotonic()
    with pytest.raises(TemplateLimitError):
        env.from_string(loops).render()
    assert time.monotonic() - started < 1


def test_a_render_past_the_deadline_stops(monkeypatch):
    monkeypatch.setattr(templating, "RENDER_DEADLINE_S", 0.0)
    with pytest.raises(TemplateLimitError, match="longer than"):
        text_env().from_string("{% for a in range(1000) %}{% endfor %}").render()


def test_each_render_gets_its_own_budget():
    template = text_env().from_string("{% for a in range(60000) %}{% endfor %}ok")
    assert [template.render() for _ in range(3)] == ["ok", "ok", "ok"]


def test_compiled_expression_refuses_huge_results():
    with pytest.raises(TemplateLimitError):
        native_env().compile_expression("'x' * 50000000")()


@pytest.mark.parametrize("env", [text_env(), native_env()], ids=["text", "native"])
def test_huge_ranges_are_refused(env):
    with pytest.raises(OverflowError):
        env.from_string("{{ range(10000000) | list }}").render()


def test_output_past_the_cap_names_the_cap():
    with pytest.raises(TemplateLimitError, match=str(MAX_OUTPUT_CHARS)):
        text_env().from_string("{% for a in range(20) %}{{ 'x' * 99999 }}{% endfor %}").render()


def test_small_templates_still_work():
    source = ("{{ 'a' ~ 'b' ~ x }}|{{ 'ab' * 3 }}|{{ 2 ** 10 }}|{{ [1] + [2] }}|{{ 'a' + 'b' }}|{{ 7 % 3 }}|"
              "{{ '%5s|%.2f' % ('x', 1.5) }}|{{ '{:>5}'.format('x') }}|{{ 'x' | center(5) }}|{{ [1, 2] | join(',') }}|"
              "{% for i in range(3) %}{{ i }}{% endfor %}|{{ 'abc'.replace('b', 'X') }}|{{ ','.join(['a', 'b']) }}")
    assert text_env().from_string(source).render(x=1) == (
        "ab1|ababab|1024|[1, 2]|ab|1|    x|1.50|    x|  x  |1,2|012|aXc|a,b")
    assert native_env().from_string("{{ 2 ** 10 }}").render() == 1024


def test_prompt_description_falls_back_on_a_huge_render():
    assert _render_description("{{ 'x' * 50000000 }}", {}) == "{{ 'x' * 50000000 }}"


def test_prompt_description_renders_block_tags_alone():
    assert _render_description("Plan.{% if topic %} Focus.{% endif %}", {"topic": "x"}) == "Plan. Focus."


async def test_gate_message_renders_block_tags_alone(capsys):
    node = HumanGateNode(stage=Stage(id="g", gate="human", present="Approve{% if topic %} it{% endif %}?"))
    with patch("builtins.input", return_value="yes"):
        await node.execute({"topic": "x"})
    assert "Approve it?" in capsys.readouterr().out
