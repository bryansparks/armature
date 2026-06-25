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