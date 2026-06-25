"""Pure per-iteration logic for the outer loop driver.

No I/O, no Harness, no traces — fully unit-testable. The runner composes
these with the real Harness and TraceStore.
"""
from armature.runtime.engine import _merge_carry_forward


def build_iteration_inputs(
    base_inputs: dict,
    iteration_num: int,
    max_iterations: int,
    carried: dict,
    inject_as: str,
) -> dict:
    """Build the inputs dict for one loop iteration.

    Injects ``_iteration`` (``{num, is_first, is_last, carry_forward}``) and the
    carried values both under ``inject_as`` and deep-merged to top level —
    mirroring ``Harness._run_with_loop`` (engine.py:730-732) so prompts can
    write ``{{ _iteration.num }}``, ``{{ prior_run.s.x }}``, or ``{{ s.x }}``.

    ``base_inputs`` is not mutated.
    """
    iteration_info = {
        "num": iteration_num,
        "is_first": iteration_num == 1,
        "is_last": iteration_num == max_iterations,
        "carry_forward": carried,
    }
    iter_inputs = dict(base_inputs)
    iter_inputs["_iteration"] = iteration_info
    if carried:
        if inject_as:
            iter_inputs[inject_as] = carried
        _merge_carry_forward(iter_inputs, carried)
    return iter_inputs


def decide_stop(
    result: dict,
    prev_result: dict | None,
    until_met: bool,
    converge: bool,
    accumulated: dict,
    budgets: dict,
) -> str | None:
    """Decide whether the loop should stop after this iteration.

    Precedence (matches spec §5): until_met → converged → budget. Returns a
    stop_reason string or ``None`` (continue). ``budgets`` is a dict that may
    contain ``max_llm_calls`` / ``max_tokens`` / ``max_wallclock`` (each
    optional, value None means unset). ``accumulated`` is
    ``{llm_calls, tokens, wall_s}``.
    """
    if until_met:
        return "until_met"
    if converge and prev_result is not None and result == prev_result:
        return "converged"
    if budgets.get("max_llm_calls") is not None and accumulated["llm_calls"] >= budgets["max_llm_calls"]:
        return "budget_llm_calls"
    if budgets.get("max_tokens") is not None and accumulated["tokens"] >= budgets["max_tokens"]:
        return "budget_tokens"
    if budgets.get("max_wallclock") is not None and accumulated["wall_s"] >= budgets["max_wallclock"]:
        return "budget_wallclock"
    return None
