"""Tests for armature.spec.context — pure governance resolution."""
from armature.spec.context import (
    MISSION_LAYER_NAME,
    EffectiveContextPolicy,
    floor_never,
    mission_layer,
    ordered_layers,
    resolve_effective_policy,
    runtime_context_keys,
)
from armature.spec.models import HarnessSpec


def _spec(**overrides) -> HarnessSpec:
    data = {"name": "wf", "stages": [{"id": "a"}, {"id": "b"}]}
    data.update(overrides)
    return HarnessSpec.model_validate(data)


def test_mission_layer_synthesized_when_mission_present():
    layer = mission_layer(_spec(mission="Do good work."))
    assert layer is not None
    assert layer.name == "mission"
    assert layer.content == "Do good work."
    assert layer.precedence == -1000


def test_mission_layer_absent_without_mission():
    assert mission_layer(_spec()) is None


def test_ordered_layers_mission_renders_last():
    spec = _spec(mission="m", context_layers=[
        {"name": "principles", "content": "p", "precedence": 5},
        {"name": "domain", "content": "d", "precedence": 10},
    ])
    assert [l.name for l in ordered_layers(spec)] == ["domain", "principles", "mission"]


def test_ordered_layers_ties_break_by_declaration_order():
    spec = _spec(context_layers=[
        {"name": "first", "content": "x"},
        {"name": "second", "content": "y"},
    ])
    assert [l.name for l in ordered_layers(spec)] == ["first", "second"]


def test_floor_never_unions_all_layers():
    spec = _spec(context_layers=[
        {"name": "a", "content": "x", "never": ["secret_stage"]},
        {"name": "b", "content": "y", "never": ["raw_pii", "secret_stage"]},
    ])
    assert floor_never(spec) == frozenset({"secret_stage", "raw_pii"})


def test_runtime_context_keys_covers_injected_and_memory_keys():
    spec = _spec(
        memory={"inject_as": "_memory", "inject_knowledge_as": "_knowledge"},
        continuation={"carry_forward": []},
    )
    keys = runtime_context_keys(spec)
    assert {"run_id", "_transcript", "_diagnostics", "_stale_memory_keys",
            "_memory_index", "prior_run", "_memory", "_knowledge"} <= keys


def test_resolve_no_policy_musts_mission_only():
    spec = _spec(mission="m", context_layers=[{"name": "p", "content": "x"}])
    pol = resolve_effective_policy(spec, spec.stages[0])
    assert pol.must == ("mission",)
    assert pol.never == frozenset()


def test_resolve_stage_never_closes_default_must():
    # F1 regression: a stage never must beat the workflow default's must
    spec = _spec(
        context_policy={"must": ["principles"]},
        context_layers=[{"name": "principles", "content": "x"}],
        stages=[{"id": "a", "context_policy": {"never": ["principles"]}}],
    )
    pol = resolve_effective_policy(spec, spec.stages[0])
    assert "principles" not in pol.must


def test_resolve_floor_never_beats_stage_must():
    # F1 regression: a layer-floor closure must beat a stage's must
    spec = _spec(
        context_layers=[
            {"name": "guard", "content": "x", "never": ["secret_rules"]},
            {"name": "secret_rules", "content": "y"},
        ],
        stages=[{"id": "a", "context_policy": {"must": ["secret_rules"]}}],
    )
    pol = resolve_effective_policy(spec, spec.stages[0])
    assert "secret_rules" not in pol.must


def test_resolve_additive_never_across_levels():
    spec = _spec(
        context_policy={"never": ["b"]},
        stages=[{"id": "a", "context_policy": {"never": ["raw_pii"]}}],
    )
    pol = resolve_effective_policy(spec, spec.stages[0])
    assert pol.never == frozenset({"b", "raw_pii"})


def test_resolve_mission_closeable():
    spec = _spec(mission="m",
                 stages=[{"id": "a", "context_policy": {"never": ["mission"]}}])
    pol = resolve_effective_policy(spec, spec.stages[0])
    assert MISSION_LAYER_NAME not in pol.must


def test_effective_policy_as_dict_is_serializable():
    p = EffectiveContextPolicy(must=("mission", "x"), never=frozenset({"b"}))
    assert p.as_dict() == {"must": ["mission", "x"], "never": ["b"]}


# ── Slice 2: work_unit objective injection (mission_source: work_unit) ──────

from armature.spec.context import work_unit_layer


def test_work_unit_layer_synthesized_with_record():
    spec = _spec(mission_source="work_unit")
    rec = {"mission": "m", "mission_objective": "Ship it.", "unit_id": "a",
           "title": "Unit A", "objective": "12 variants.", "requires": ["b"],
           "posture": "delegated", "state": "in_progress", "attempts": 1}
    layers = ordered_layers(spec, rec)
    names = [l.name for l in layers]
    assert "mission" in names and "work_unit" in names
    wu = next(l for l in layers if l.name == "work_unit")
    assert "Unit A" in wu.content and "12 variants." in wu.content
    assert "b" in wu.content


def test_work_unit_layer_absent_without_record_or_opt_in():
    spec = _spec(mission_source="work_unit")
    assert work_unit_layer(spec, None) is None
    spec2 = _spec(mission="static", mission_source="static")
    assert work_unit_layer(spec2, {"title": "x", "objective": ""}) is None


def test_mission_layer_content_from_record_when_sourced():
    spec = _spec(mission="static string", mission_source="work_unit")
    rec = {"mission": "m", "mission_objective": "From the record.",
           "unit_id": "a", "title": "A", "objective": "", "requires": []}
    m = mission_layer(spec, rec)
    assert m is not None and "From the record." in m.content


def test_mission_layer_falls_back_to_static_without_record():
    spec = _spec(mission="static string", mission_source="work_unit")
    m = mission_layer(spec, None)
    assert m is not None and m.content == "static string"


def test_runtime_context_keys_include_work_unit_when_sourced():
    spec = _spec(mission_source="work_unit")
    assert "work_unit" in runtime_context_keys(spec)
    assert "work_unit" not in runtime_context_keys(_spec())


def test_never_can_target_work_unit_layer():
    spec = _spec(mission_source="work_unit")
    stage = HarnessSpec.model_validate({
        "name": "s",
        "stages": [{"id": "x", "context_policy": {"never": ["work_unit"]}}],
    }).stages[0]
    eff = resolve_effective_policy(spec, stage, {"title": "A", "objective": ""})
    assert "work_unit" in eff.never and "work_unit" not in eff.must


def test_work_unit_layer_reserved_name_rejected():
    from armature.spec.validator import validate_spec
    spec = HarnessSpec.model_validate({
        "name": "s",
        "context_layers": [{"name": "work_unit", "precedence": 1, "content": "hi"}],
        "stages": [{"id": "x"}],
    })
    errors = validate_spec(spec, strict=False)
    assert any(e.code == "RESERVED_CONTEXT_LAYER_NAME" and "work_unit" in e.message
               for e in errors)
