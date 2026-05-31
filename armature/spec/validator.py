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
        if stage.fan_out is not None and stage.fan_out < 1:
            errors.append(SpecError(
                code="INVALID_FAN_OUT",
                message=f"fan_out must be >= 1, got {stage.fan_out}",
                stage_id=stage.id,
            ))
        if stage.inject_file_as is not None and stage.partition_source is None:
            errors.append(SpecError(
                code="INJECT_FILE_MISSING_PARTITION_SOURCE",
                message="inject_file_as only has effect inside a fan-out stage; partition_source is missing",
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
    # Use model_dump so custom tiers (e.g. 'synthesis') beyond the five
    # standard names are also recognised as defined.
    defined_tiers = {
        name for name, val in spec.model_tiers.model_dump().items()
        if val is not None
    }
    for stage in spec.stages:
        if stage.role is not None:
            if stage.role.model_tier is not None:
                if stage.role.model_tier not in defined_tiers:
                    errors.append(SpecError(
                        code="UNDEFINED_MODEL_TIER",
                        message=f"Role references model_tier '{stage.role.model_tier}' which is not defined in model_tiers",
                        stage_id=stage.id,
                    ))
            elif defined_tiers:
                # No explicit model_tier — will resolve through role_type_defaults.
                # Only flag misconfiguration when the spec has at least one tier defined;
                # a spec with no model_tiers at all is not necessarily broken (e.g. tests).
                default_tier = getattr(spec.role_type_defaults, stage.role.type.value, None)
                if default_tier and default_tier not in defined_tiers:
                    errors.append(SpecError(
                        code="DEFAULT_TIER_NOT_CONFIGURED",
                        message=(
                            f"Role type '{stage.role.type.value}' defaults to tier '{default_tier}' "
                            f"(from role_type_defaults) but that tier is not defined in model_tiers"
                        ),
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

    # ── Contract.outputs reference valid stage ids and have a key ─────────
    for i, out in enumerate(spec.contracts.outputs):
        if "stage" not in out:
            errors.append(SpecError(
                code="CONTRACT_OUTPUT_MISSING_STAGE",
                message=f"contracts.outputs[{i}] is missing the 'stage' field",
                stage_id=None,
            ))
        elif out["stage"] not in stage_ids:
            errors.append(SpecError(
                code="CONTRACT_OUTPUT_UNDEFINED_STAGE",
                message=(
                    f"contracts.outputs[{i}] references unknown stage '{out['stage']}'"
                ),
                stage_id=None,
            ))
        if "key" not in out:
            errors.append(SpecError(
                code="CONTRACT_OUTPUT_MISSING_KEY",
                message=f"contracts.outputs[{i}] is missing the 'key' field",
                stage_id=None,
            ))

    # ── Cross-stage signature type compatibility ──────────────────────────
    # Build a map of stage_id → Signature for quick lookup.
    sig_by_id = {s.id: s.signature for s in spec.stages if s.signature is not None}

    # Determine keys that are valid workflow-level inputs (from contracts.inputs).
    workflow_input_keys = {
        inp["name"] for inp in spec.contracts.inputs if "name" in inp
    }

    for stage in spec.stages:
        if stage.signature is None or not stage.depends_on:
            continue
        for dep_id in stage.depends_on:
            dep_sig = sig_by_id.get(dep_id)
            if dep_sig is None:
                continue
            # Check type compatibility for shared keys.
            for key, downstream_type in stage.signature.input.items():
                if key not in dep_sig.output:
                    continue
                upstream_type = dep_sig.output[key]
                if upstream_type != downstream_type:
                    errors.append(SpecError(
                        code="SIGNATURE_TYPE_MISMATCH",
                        message=(
                            f"Key '{key}': stage '{dep_id}' outputs type '{upstream_type}' "
                            f"but stage '{stage.id}' expects '{downstream_type}'"
                        ),
                        stage_id=stage.id,
                    ))

        # Check that every input key is resolvable at runtime.
        # Fan-out stages inject partition variables dynamically — skip them entirely.
        # For all other stages, the Armature context is cumulative: every stage that
        # has already run in the DAG contributes its output to the shared context,
        # so any stage ID in the workflow is a valid key in addition to contract inputs
        # and explicit output fields from upstream signatures.
        if stage.depends_on and not stage.fan_out:
            all_upstream_output_keys: set[str] = set()
            for dep_id in stage.depends_on:
                dep_sig = sig_by_id.get(dep_id)
                if dep_sig is not None:
                    all_upstream_output_keys.update(dep_sig.output.keys())
            for key in stage.signature.input:
                valid = (
                    key in stage_ids               # key is any stage ID (context is cumulative)
                    or key in all_upstream_output_keys  # key is an output field of a dep stage
                    or key in workflow_input_keys       # key is declared in contracts.inputs
                )
                if not valid:
                    errors.append(SpecError(
                        code="UNDEFINED_SIGNATURE_INPUT",
                        message=(
                            f"Stage '{stage.id}' signature.input key '{key}' is not a "
                            f"stage ID, not output by any depends_on stage, "
                            f"and is not in contracts.inputs"
                        ),
                        stage_id=stage.id,
                    ))

    # ── Only-tighten safety rule composition (KYA-inspired) ──────────────────
    # An allow rule targeting the same tool as a block rule (or wildcard block)
    # potentially loosens a restriction — flag as a conflict.
    block_tools = {r.tool for r in spec.safety_rules if r.action == "block"}
    allow_rules = [r for r in spec.safety_rules if r.action == "allow"]
    for allow_rule in allow_rules:
        # A specific allow rule conflicts with any wildcard block
        # A specific allow rule for tool X conflicts with a specific block for tool X
        if "*" in block_tools or allow_rule.tool in block_tools:
            errors.append(SpecError(
                code="CONFLICTING_SAFETY_RULES",
                message=(
                    f"Safety rule with action='allow' for tool '{allow_rule.tool}' "
                    f"may loosen an existing block rule — "
                    f"review rule ordering (only-tighten principle)"
                ),
                stage_id=None,
            ))

    if strict and errors:
        raise SpecValidationError(errors)

    return errors
