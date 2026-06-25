from armature.loop.logic import build_iteration_inputs, decide_stop


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


def _acc(llm=0, tok=0, wall=0.0):
    return {"llm_calls": llm, "tokens": tok, "wall_s": wall}


def test_stop_until_met_wins_over_everything():
    acc = _acc(llm=100, tok=1000, wall=999.0)
    budgets = {"max_llm_calls": 5, "max_tokens": 10, "max_wallclock": 1.0}
    assert decide_stop({"a": 1}, {"a": 0}, True, True, acc, budgets) == "until_met"


def test_stop_converged_when_until_false_and_results_equal():
    acc = _acc()
    assert decide_stop({"a": 1}, {"a": 1}, False, True, acc, {}) == "converged"


def test_stop_converged_skipped_when_prev_none():
    assert decide_stop({"a": 1}, None, False, True, _acc(), {}) is None


def test_stop_budget_llm_calls():
    acc = _acc(llm=8)
    assert decide_stop({}, None, False, False, acc, {"max_llm_calls": 5}) == "budget_llm_calls"


def test_stop_budget_tokens():
    acc = _acc(tok=2000)
    assert decide_stop({}, None, False, False, acc, {"max_tokens": 1000}) == "budget_tokens"


def test_stop_budget_wallclock():
    acc = _acc(wall=30.0)
    assert decide_stop({}, None, False, False, acc, {"max_wallclock": 10.0}) == "budget_wallclock"


def test_stop_none_when_nothing_trips():
    acc = _acc(llm=4, tok=100, wall=1.0)
    budgets = {"max_llm_calls": 50, "max_tokens": 10000, "max_wallclock": 60.0}
    assert decide_stop({"a": 1}, {"a": 0}, False, False, acc, budgets) is None