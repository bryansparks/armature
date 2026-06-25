from armature.loop.logic import build_iteration_inputs


def test_inputs_first_iteration_no_carry():
    out = build_iteration_inputs({"topic": "x"}, 1, 5, {}, "prior_run")
    assert out["topic"] == "x"
    assert out["_iteration"]["num"] == 1
    assert out["_iteration"]["is_first"] is True
    assert out["_iteration"]["is_last"] is False
    assert out["_iteration"]["carry_forward"] == {}
    assert "prior_run" not in out


def test_inputs_carries_under_inject_as_and_top_level():
    carried = {"s": {"x": 1}}
    out = build_iteration_inputs({"topic": "x"}, 2, 5, carried, "prior_run")
    assert out["_iteration"]["num"] == 2
    assert out["_iteration"]["is_first"] is False
    assert out["prior_run"] == {"s": {"x": 1}}
    # carry is also deep-merged to top level (mirrors _run_with_loop)
    assert out["s"] == {"x": 1}


def test_inputs_is_last_when_iteration_equals_max():
    out = build_iteration_inputs({}, 3, 3, {}, "prior_run")
    assert out["_iteration"]["is_last"] is True


def test_inputs_does_not_mutate_base_inputs():
    base = {"topic": "x"}
    build_iteration_inputs(base, 2, 5, {"s": {"x": 1}}, "prior_run")
    assert base == {"topic": "x"}  # base untouched