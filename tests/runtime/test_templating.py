"""Every template Armature renders goes through a sandbox."""
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
from armature.runtime.templating import SandboxedNativeEnvironment, native_env, text_env
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
