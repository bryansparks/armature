"""Tests for adapter-related spec validation."""
import pytest

from armature.runtime.engine import Harness
from armature.spec.loader import load_spec
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, ToolCallConfig, ModelTiers, ModelTierConfig,
    SkillDef, SkillAdapterRef, AdapterFactoryConfig,
)
from armature.spec.validator import validate_spec, SpecError, SpecValidationError


def codes(errors: list[SpecError]) -> set[str]:
    return {e.code for e in errors}


def _small_tiers() -> ModelTiers:
    return ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))


def test_unknown_adapter_backend_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(backend="unknown"),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNKNOWN_ADAPTER_BACKEND" in codes(errors)


def test_known_adapter_backend_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(backend="modal"),
    )
    errors = validate_spec(spec, strict=False)
    assert "UNKNOWN_ADAPTER_BACKEND" not in codes(errors)


def test_adapter_factory_no_base_model_warning():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        adapter_factory=AdapterFactoryConfig(backend="modal"),
    )
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "ADAPTER_FACTORY_NO_BASE_MODEL" in warning_codes


def test_adapter_base_model_mismatch_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd"),
            ),
        },
        adapter_factory=AdapterFactoryConfig(backend="modal", base_model="different/model"),
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_BASE_MODEL_MISMATCH" in codes(errors)


def test_adapter_base_model_match_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd"),
            ),
        },
        adapter_factory=AdapterFactoryConfig(backend="modal", base_model="gpt-4o-mini"),
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_BASE_MODEL_MISMATCH" not in codes(errors)


def test_adapter_no_fallback_detected():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                adapter=SkillAdapterRef(name="tdd", fallback="text"),
            ),
        },
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_NO_FALLBACK" in codes(errors)


def test_adapter_with_text_fallback_passes():
    spec = HarnessSpec(
        name="wf",
        stages=[Stage(id="s", tool_call=ToolCallConfig(name="t"), depends_on=[])],
        model_tiers=_small_tiers(),
        skill_library={
            "tdd": SkillDef(
                id="tdd",
                description="TDD",
                content="do tdd",
                adapter=SkillAdapterRef(name="tdd", fallback="text"),
            ),
        },
    )
    errors = validate_spec(spec, strict=False)
    assert "ADAPTER_NO_FALLBACK" not in codes(errors)


CMD_SPEC = """name: t
contracts:
  inputs: [{name: text}, {name: other_input}]
adapters:
  say: {name: say, type: script, cmd: "CMD"}
stages:
  - id: a
    adapter: say
"""


def _cmd_spec(tmp_path, cmd: str, header: str = "# armature: stored-workflow\n"):
    path = tmp_path / "wf.yaml"
    path.write_text(header + CMD_SPEC.replace("CMD", cmd))
    return path


@pytest.mark.parametrize("cmd", ["echo '{{ text }}'", "echo {{ trigger_payload.user }}",
                                 "{% if other_input %}echo x{% endif %}", "{% set t = text %}echo {{ t }}",
                                 "echo {{ a.summary }}", "echo {{ _transcript }}"])
def test_inline_cmd_may_not_render_a_value(tmp_path, cmd):
    path = _cmd_spec(tmp_path, cmd)
    errors = validate_spec(load_spec(path), strict=False)
    assert [e.code for e in errors if e.code == "ADAPTER_CMD_INPUT"] == ["ADAPTER_CMD_INPUT"]
    assert "ARMATURE_WITH or ARMATURE_CONTEXT" in next(e.message for e in errors if e.code == "ADAPTER_CMD_INPUT")


def test_inline_cmd_may_render_only_the_run_id(tmp_path):
    path = _cmd_spec(tmp_path, "echo {{ run_id }}")
    assert "ADAPTER_CMD_INPUT" not in codes(validate_spec(load_spec(path), strict=False))


def test_pack_cmd_is_exempt():
    from armature.spec.models import PACK_ADAPTER_CONTEXT, Adapter
    adapter = Adapter.model_validate({"name": "p", "type": "script", "cmd": "echo {{ text }}", "pack_dir": "/p"},
                                     context={PACK_ADAPTER_CONTEXT: True})
    spec = HarnessSpec(name="t", stages=[], adapters={"p": adapter})
    assert "ADAPTER_CMD_INPUT" not in codes(validate_spec(spec, strict=False))


IF_BLOCK = """name: t
contracts:
  inputs: [{name: text}, {name: mode}]
adapters:
  sh: {name: sh, type: script, cmd: "echo {{ text }}"}
stages:
{% if mode %}
  - id: a
    adapter: sh
{% endif %}
"""


IF_ELSE_ADAPTER = """name: t
contracts:
  inputs: [{name: text}, {name: mode}]
adapters:
{% if mode %}
  sh: {name: sh, type: script, cmd: "echo {{ text }}", parse: json}
{% else %}
  sh: {name: sh, type: script, cmd: "echo safe", parse: json}
{% endif %}
stages:
  - {id: a, adapter: sh, depends_on: []}
"""
IF_ONLY_ADAPTER = IF_ELSE_ADAPTER.replace(
    '{% else %}\n  sh: {name: sh, type: script, cmd: "echo safe", parse: json}\n', "")


@pytest.mark.parametrize("body", [IF_BLOCK, IF_ELSE_ADAPTER, IF_ONLY_ADAPTER], ids=["if-stage", "if-else", "if-only"])
def test_text_spec_whose_adapters_depend_on_inputs_is_refused_at_load(tmp_path, body):
    path = tmp_path / "wf.yml"
    path.write_text(body)
    marker = tmp_path / "pwned"
    with pytest.raises(ValueError, match="ADAPTER_CMD_INPUT"):
        load_spec(path, {"text": f"$(touch {marker})", "mode": "1"})
    assert not marker.exists()


def test_text_spec_with_input_free_cmds_loads_with_inputs(tmp_path):
    path = tmp_path / "wf.yml"
    path.write_text(IF_BLOCK.replace("echo {{ text }}", "echo {{ run_id }}"))
    assert load_spec(path, {"text": "x", "mode": "1"}).adapters["sh"].cmd == "echo {{ run_id }}"


def test_text_spec_that_never_parses_without_inputs_is_refused(tmp_path):
    path = tmp_path / "wf.yml"
    path.write_text(IF_BLOCK.replace("{% if mode %}", "{% if mode.startswith('1') %}"))
    with pytest.raises(ValueError, match="ADAPTER_CMD_INPUT"):
        load_spec(path, {"text": "x", "mode": "1"})


def test_stored_shell_injection_through_an_input_is_refused_before_it_runs(tmp_path):
    path = _cmd_spec(tmp_path, "echo '{{ text }}'")
    marker = tmp_path / "pwned"
    inputs = {"text": f"x'; touch {marker}; echo '", "other_input": ""}
    spec = load_spec(path, inputs)
    assert spec.adapters["say"].cmd == "echo '{{ text }}'"
    with pytest.raises(SpecValidationError, match="ADAPTER_CMD_INPUT"):
        Harness(spec=spec, session_dir=tmp_path / "s")
    assert not marker.exists()


def test_text_shell_injection_through_an_input_is_refused_at_load(tmp_path):
    path = _cmd_spec(tmp_path, "echo '{{ text }}'", "")
    with pytest.raises(ValueError, match="ADAPTER_CMD_INPUT"):
        load_spec(path, {"text": "x'; touch pwned; echo '", "other_input": ""})


def test_cmd_error_points_at_the_adapter_cmd(tmp_path):
    errors = validate_spec(load_spec(_cmd_spec(tmp_path, "echo {{ text }}")), strict=False)
    assert next(e.path for e in errors if e.code == "ADAPTER_CMD_INPUT") == ["adapters", "say", "cmd"]
