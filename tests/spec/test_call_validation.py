"""Validator checks for call: stages, typed contracts, and loop.timeout_s."""
from armature.spec.calls import MappingCallResolver
from armature.spec.models import (
    CallRef, Contract, HarnessSpec, IterationConfig, Role, RoleType, Stage,
)
from armature.spec.validator import validate_spec

CALLEE = """# armature: stored-workflow
name: fixer
contracts:
  inputs:
    - {name: repo, type: string, required: true}
    - {name: attempts, type: integer, default: 3}
    - {name: verbose, type: boolean}
stages:
  - id: s
    gate: human
"""


def _codes(spec, resolver=None):
    return sorted(e.code for e in validate_spec(spec, strict=False, call_resolver=resolver)
                  if e.severity == "error")


def _spec(stage: Stage, **kw) -> HarnessSpec:
    return HarnessSpec(name="caller", stages=[stage], **kw)


RESOLVER = MappingCallResolver({("fixer", 1): CALLEE}, {"fixer": 1})


def test_call_stage_is_an_execution_type():
    assert _codes(_spec(Stage(id="c", call=CallRef(workflow="fixer")))) == []


def test_call_with_valid_inputs_passes():
    stage = Stage(id="c", call=CallRef(workflow="fixer"),
                  **{"with": {"repo": "{{ repo }}", "attempts": "5", "verbose": True}})
    assert _codes(_spec(stage), RESOLVER) == []


def test_call_rejects_unknown_and_missing_inputs():
    stage = Stage(id="c", call=CallRef(workflow="fixer"), **{"with": {"colour": "red"}})
    assert _codes(_spec(stage), RESOLVER) == ["CALL_MISSING_INPUT", "CALL_UNKNOWN_INPUT"]


def test_call_rejects_literal_of_wrong_type():
    stage = Stage(id="c", call=CallRef(workflow="fixer"),
                  **{"with": {"repo": "r", "attempts": "many"}})
    assert _codes(_spec(stage), RESOLVER) == ["CALL_INPUT_TYPE"]


def test_call_rejects_self_bad_name_bad_version_and_mixed_execution():
    assert _codes(_spec(Stage(id="c", call=CallRef(workflow="caller")))) == ["CALL_SELF"]
    assert _codes(_spec(Stage(id="c", call=CallRef(workflow="../x")))) == ["CALL_INVALID_WORKFLOW"]
    assert _codes(_spec(Stage(id="c", call=CallRef(workflow="fixer", version=0)))) == ["CALL_INVALID_VERSION"]
    mixed = Stage(id="c", call=CallRef(workflow="fixer"), gate="human")
    assert _codes(_spec(mixed)) == ["CALL_CONFLICTING_EXECUTION"]


def test_call_unresolved_with_resolver():
    assert _codes(_spec(Stage(id="c", call=CallRef(workflow="ghost"))), RESOLVER) == ["CALL_UNRESOLVED"]


def test_with_still_needs_an_adapter_or_call():
    role = Role(name="r", type=RoleType.WORKER, description="d")
    assert _codes(_spec(Stage(id="c", role=role, **{"with": {"a": 1}}))) == ["INVALID_WITH"]


def test_typed_input_declarations_are_checked():
    spec = _spec(Stage(id="g", gate="human"), contracts=Contract(inputs=[
        {"name": "a", "type": "flag"},
        {"name": "b", "type": "boolean", "default": "yes"},
        {"name": "c", "type": "string", "enum": ["x"], "default": "x"},
    ]))
    assert _codes(spec) == ["CONTRACT_INVALID_DEFAULT", "CONTRACT_INVALID_TYPE"]


def test_typed_outputs_need_from_and_skip_stage_key_checks():
    spec = _spec(Stage(id="g", gate="human"), contracts=Contract(outputs=[
        {"name": "ok", "type": "boolean", "from": "{{ g.approved }}"},
        {"name": "bad", "type": "boolean"},
        {"stage": "g", "key": "approved"},
    ]))
    assert _codes(spec) == ["CONTRACT_OUTPUT_MISSING_FROM"]


def test_loop_timeout_must_be_positive():
    stage = Stage(id="g", gate="human", loop=IterationConfig(timeout_s=0))
    assert _codes(_spec(stage)) == ["LOOP_INVALID_TIMEOUT"]


def test_templated_call_workflow_is_rejected():
    errors = validate_spec(_spec(Stage(id="c", call=CallRef(workflow="{{ name }}"))), strict=False)
    assert [(e.code, e.message) for e in errors if e.severity == "error"] == [
        ("CALL_INVALID_WORKFLOW", "call.workflow holds a template; a call names a fixed workflow")]
