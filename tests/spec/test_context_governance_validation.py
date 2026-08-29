"""Validation tests for context governance codes."""
from armature.spec.models import HarnessSpec
from armature.spec.validator import validate_spec

GOVERNANCE_CODES = {
    "RESERVED_CONTEXT_LAYER_NAME", "UNKNOWN_CONTEXT_LAYER",
    "UNKNOWN_CONTEXT_SOURCE", "CONTEXT_POLICY_CONTRADICTS_FLOOR",
    "NEVER_BLOCKS_PARTITION_SOURCE", "CONTEXT_TRANSIT_LEAK_RISK",
}

_WORKER = {"name": "W", "type": "worker", "description": "d"}


def _spec(**overrides) -> HarnessSpec:
    data = {
        "name": "v",
        "model_tiers": {"small": {"provider": "mock", "model": "m"}},
        "role_type_defaults": {"worker": "small"},
        "contracts": {"inputs": [{"name": "raw_pii"}]},
        "stages": [
            {"id": "researcher", "role": _WORKER},
            {"id": "analyst", "role": _WORKER,
             "context_policy": {"must": ["principles"]}},
        ],
        "context_layers": [{"name": "principles", "content": "Be terse."}],
    }
    data.update(overrides)
    return HarnessSpec.model_validate(data)


def _codes(spec) -> set[str]:
    return {e.code for e in validate_spec(spec, strict=False)}


def test_valid_governance_produces_no_governance_codes():
    assert _codes(_spec()) & GOVERNANCE_CODES == set()


def test_reserved_layer_name_mission_rejected():
    spec = _spec(context_layers=[{"name": "mission", "content": "hijack"}])
    assert "RESERVED_CONTEXT_LAYER_NAME" in _codes(spec)


def test_unknown_must_layer_rejected():
    assert "UNKNOWN_CONTEXT_LAYER" in _codes(_spec(context_policy={"must": ["nonexistent"]}))


def test_unknown_never_source_rejected():
    spec = _spec(context_policy={"never": ["not_a_stage_input_layer_or_key"]})
    assert "UNKNOWN_CONTEXT_SOURCE" in _codes(spec)


def test_layer_never_unknown_source_rejected():
    spec = _spec(context_layers=[
        {"name": "guard", "content": "x", "never": ["bogus_target"]}])
    assert "UNKNOWN_CONTEXT_SOURCE" in _codes(spec)


def test_never_accepts_stage_ids_inputs_layers_and_injected_keys():
    spec = _spec(context_policy={
        "never": ["researcher", "raw_pii", "principles", "_transcript"]})
    assert "UNKNOWN_CONTEXT_SOURCE" not in _codes(spec)


def test_must_overlapping_floor_never_rejected():
    spec = _spec(
        context_layers=[
            {"name": "guard", "content": "x", "never": ["secret_rules"]},
            {"name": "secret_rules", "content": "y"},
        ],
        context_policy={"must": ["secret_rules"]},
    )
    assert "CONTEXT_POLICY_CONTRADICTS_FLOOR" in _codes(spec)


def test_same_policy_must_and_never_rejected():
    spec = _spec(stages=[
        {"id": "researcher", "role": _WORKER},
        {"id": "analyst", "role": _WORKER,
         "context_policy": {"must": ["principles"], "never": ["principles"]}},
    ])
    assert "CONTEXT_POLICY_CONTRADICTS_FLOOR" in _codes(spec)


def test_default_must_overlapping_default_never_rejected():
    spec = _spec(context_policy={"must": ["principles"], "never": ["principles"]})
    assert "CONTEXT_POLICY_CONTRADICTS_FLOOR" in _codes(spec)


def test_stage_never_closing_default_must_is_legal():
    # adds-never-overrides: a stage may narrow its own view of a default-must'd layer
    spec = _spec(
        context_policy={"must": ["principles"]},
        stages=[
            {"id": "researcher", "role": _WORKER},
            {"id": "analyst", "role": _WORKER,
             "context_policy": {"never": ["principles"]}},
        ],
    )
    assert "CONTEXT_POLICY_CONTRADICTS_FLOOR" not in _codes(spec)


def test_never_blocks_partition_source_warns():
    spec = _spec(stages=[
        {"id": "researcher", "role": _WORKER},
        {"id": "fan", "depends_on": ["researcher"], "fan_out": 3, "fan_in": "list",
         "partition_key": "item", "partition_source": "{{ researcher.queries }}",
         "tool_call": {"name": "x", "args": {}},
         "context_policy": {"never": ["researcher"]}},
    ])
    errors = validate_spec(spec, strict=False)
    assert "NEVER_BLOCKS_PARTITION_SOURCE" in {e.code for e in errors}
    for e in errors:
        if e.code == "NEVER_BLOCKS_PARTITION_SOURCE":
            assert e.severity == "warning"


def test_workflow_default_never_triggers_partition_source_warning():
    # Blind spot fix: a stage governed only by the workflow-level never (no
    # stage.context_policy of its own) must still be analyzed for warnings.
    spec = _spec(
        context_policy={"never": ["researcher"]},
        stages=[
            {"id": "researcher", "role": _WORKER},
            {"id": "fan", "depends_on": ["researcher"], "fan_out": 3, "fan_in": "list",
             "partition_key": "item", "partition_source": "{{ researcher.queries }}",
             "tool_call": {"name": "x", "args": {}}},
        ],
    )
    assert "NEVER_BLOCKS_PARTITION_SOURCE" in _codes(spec)


def test_never_blocks_partition_source_not_triggered_for_other_stages():
    spec = _spec(stages=[
        {"id": "researcher", "role": _WORKER},
        {"id": "fan", "depends_on": ["researcher"], "fan_out": 3, "fan_in": "list",
         "partition_key": "item", "partition_source": "{{ researcher.queries }}",
         "tool_call": {"name": "x", "args": {}},
         "context_policy": {"never": ["raw_pii"]}},
    ])
    assert "NEVER_BLOCKS_PARTITION_SOURCE" not in _codes(spec)


def test_context_transit_leak_risk_warns():
    spec = _spec(stages=[
        {"id": "researcher", "role": _WORKER},
        {"id": "middle", "role": _WORKER, "depends_on": ["researcher"]},
        {"id": "analyst", "role": _WORKER, "depends_on": ["middle"],
         "context_policy": {"never": ["researcher"]}},
    ])
    errors = validate_spec(spec, strict=False)
    assert "CONTEXT_TRANSIT_LEAK_RISK" in {e.code for e in errors}
    for e in errors:
        if e.code == "CONTEXT_TRANSIT_LEAK_RISK":
            assert e.severity == "warning"


def test_context_transit_leak_risk_not_triggered_when_intermediate_also_closes_source():
    # False positive fix: if the intermediate (middle) also closes the same
    # source (researcher) in its own never, no leak reaches analyst through
    # it — the user already closed the leak.
    spec = _spec(stages=[
        {"id": "researcher", "role": _WORKER},
        {"id": "middle", "role": _WORKER, "depends_on": ["researcher"],
         "context_policy": {"never": ["researcher"]}},
        {"id": "analyst", "role": _WORKER, "depends_on": ["middle"],
         "context_policy": {"never": ["researcher"]}},
    ])
    assert "CONTEXT_TRANSIT_LEAK_RISK" not in _codes(spec)
