"""Spec validation: catch common errors before any LLM call is made.

Call `validate_spec(spec)` and handle the returned list of `SpecError`.
`strict=True` raises `SpecValidationError` on the first error found.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from armature.spec.models import HarnessSpec


@dataclass
class SpecError:
    code: str        # machine-readable tag, e.g. "UNDEFINED_DEPENDENCY"
    message: str     # human-readable description
    stage_id: str | None = None


class SpecValidationError(ValueError):
    def __init__(self, errors: list[SpecError]):
        self.errors = errors
        lines = "\n".join(f"  [{e.code}] stage={e.stage_id!r}: {e.message}" for e in errors)
        super().__init__(f"Spec validation failed ({len(errors)} error(s)):\n{lines}")


def validate_spec(spec: HarnessSpec, *, strict: bool = True) -> list[SpecError]:
    """Validate a HarnessSpec and return a list of errors (empty = valid).

    When strict=True (default), raises SpecValidationError if any errors found.
    When strict=False, returns the list without raising.
    """
    errors: list[SpecError] = []
    stage_ids = {s.id for s in spec.stages}

    # ── Duplicate stage IDs ────────────────────────────────────────────────
    seen: set[str] = set()
    for stage in spec.stages:
        if stage.id in seen:
            errors.append(SpecError(
                code="DUPLICATE_STAGE_ID",
                message=f"Stage id '{stage.id}' is defined more than once",
                stage_id=stage.id,
            ))
        seen.add(stage.id)

    # ── Undefined depends_on references ───────────────────────────────────
    for stage in spec.stages:
        for dep in stage.depends_on:
            if dep not in stage_ids:
                errors.append(SpecError(
                    code="UNDEFINED_DEPENDENCY",
                    message=f"depends_on references unknown stage '{dep}'",
                    stage_id=stage.id,
                ))

    # ── Cycle detection ────────────────────────────────────────────────────
    try:
        from armature.runtime.dag import topological_order
        deps = {s.id: s.depends_on for s in spec.stages}
        topological_order(deps)
    except ValueError:
        errors.append(SpecError(
            code="CIRCULAR_DEPENDENCY",
            message="Stage dependencies form a cycle",
            stage_id=None,
        ))

    # ── Undefined adapter references ───────────────────────────────────────
    for stage in spec.stages:
        if stage.adapter is not None and stage.adapter not in spec.adapters:
            errors.append(SpecError(
                code="UNDEFINED_ADAPTER",
                message=f"Stage references adapter '{stage.adapter}' which is not defined in spec.adapters",
                stage_id=stage.id,
            ))

    # ── Stage has no execution type ────────────────────────────────────────
    for stage in spec.stages:
        has_exec = any([
            stage.role is not None,
            stage.tool_call is not None,
            stage.adapter is not None,
            stage.gate is not None,
            stage.subagent_spec is not None,
        ])
        if not has_exec:
            errors.append(SpecError(
                code="NO_EXECUTION_TYPE",
                message="Stage has no role, tool_call, adapter, gate, or subagent_spec — it will never execute",
                stage_id=stage.id,
            ))

    # ── fan_out requires partition_source ─────────────────────────────────
    for stage in spec.stages:
        if stage.fan_out is not None and stage.partition_source is None:
            errors.append(SpecError(
                code="FAN_OUT_MISSING_PARTITION_SOURCE",
                message="Stage has fan_out set but partition_source is missing",
                stage_id=stage.id,
            ))
        if stage.partition_source is not None and stage.fan_out is None:
            errors.append(SpecError(
                code="PARTITION_SOURCE_MISSING_FAN_OUT",
                message="Stage has partition_source set but fan_out is missing",
                stage_id=stage.id,
            ))

    # ── on_fail.loop points to a valid stage ──────────────────────────────
    for stage in spec.stages:
        if stage.on_fail and stage.on_fail.loop:
            loop_stage = stage.on_fail.loop.stage
            if loop_stage not in stage_ids:
                errors.append(SpecError(
                    code="UNDEFINED_LOOP_STAGE",
                    message=f"on_fail.loop references unknown stage '{loop_stage}'",
                    stage_id=stage.id,
                ))

    # ── Model tier references exist ───────────────────────────────────────
    defined_tiers = {
        name for name in ("tiny", "small", "medium", "large", "frontier")
        if getattr(spec.model_tiers, name) is not None
    }
    for stage in spec.stages:
        if stage.role is not None and stage.role.model_tier is not None:
            if stage.role.model_tier not in defined_tiers:
                errors.append(SpecError(
                    code="UNDEFINED_MODEL_TIER",
                    message=f"Role references model_tier '{stage.role.model_tier}' which is not defined in model_tiers",
                    stage_id=stage.id,
                ))

    # ── Contract.inputs entries have required name field ──────────────────
    for i, inp in enumerate(spec.contracts.inputs):
        if "name" not in inp:
            errors.append(SpecError(
                code="CONTRACT_INPUT_MISSING_NAME",
                message=f"contracts.inputs[{i}] is missing the 'name' field",
                stage_id=None,
            ))

    if strict and errors:
        raise SpecValidationError(errors)

    return errors
