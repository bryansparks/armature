"""Carry-forward resolution for the outer loop driver.

Pure functions only — no I/O, no Harness. Mirrors the carry logic in
``Harness._run_with_loop`` (engine.py:719-732) lifted to the inputs level.
"""
import copy

from armature.runtime.engine import _resolve_dot_path, _set_nested_key


def resolve_carry(result: dict | None, carry_paths: str) -> dict:
    """Resolve carried values from the prior iteration's result dict.

    carry_paths:
      ``"*"``  -> a deep copy of the whole result dict.
      ``"a.b,c.d"`` -> only ``a.b`` and ``c.d`` (dot-paths into the result),
                       nested into the output via ``_set_nested_key``.

    Missing or None-valued paths are skipped (no raise). ``result`` that is
    None or empty yields ``{}`` (the first iteration carries nothing).
    """
    if not result:
        return {}
    if carry_paths == "*":
        return copy.deepcopy(result)
    carry: dict = {}
    for path in carry_paths.split(","):
        path = path.strip()
        if not path:
            continue
        val = _resolve_dot_path(result, path)
        if val is not None:
            _set_nested_key(carry, path, val)
    return carry
