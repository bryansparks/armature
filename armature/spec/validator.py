"""Spec validation: catch common errors before any LLM call is made.

Call `validate_spec(spec)` and handle the returned list of `SpecError`.
`strict=True` raises `SpecValidationError` on the first error found.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from armature.spec.models import HarnessSpec
from armature.spec.context import (
    MISSION_LAYER_NAME, floor_never, runtime_context_keys,
)

_PARTITION_VAR_RE = re.compile(r"\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class SpecError:
    code: str        # machine-readable tag, e.g. "UNDEFINED_DEPENDENCY"
    message: str     # human-readable description
    stage_id: str | None = None
    severity: str = "error"  # "error" or "warning"


class SpecValidationError(ValueError):
    def __init__(self, errors: list[SpecError]):
        self.errors = errors
        lines = "\n".join(f"  [{e.code}] stage={e.stage_id!r}: {e.message}" for e in errors)
        super().__init__(f"Spec validation failed ({len(errors)} error(s)):\n{lines}")


def _harness_injected_keys_set() -> set[str]:
    """Static accessor for tests — the base set always injected by the harness."""
    return {"run_id", "_transcript", "_diagnostics", "_stale_memory_keys", "_memory_index"}


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
            stage.agent is not None,  # resolved to role at load time; valid before resolution
        ])
        if not has_exec:
            errors.append(SpecError(
                code="NO_EXECUTION_TYPE",
                message="Stage has no role, tool_call, adapter, gate, subagent_spec, or agent — it will never execute",
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
    _LOW_TIER_NAMES: frozenset[str] = frozenset({"tiny", "small"})
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

    # ── guided_json stages on low tiers risk schema failures ─────────────
    for stage in spec.stages:
        if stage.output_mode != "guided_json" or stage.role is None or not defined_tiers:
            continue
        effective_tier = stage.role.model_tier
        if effective_tier is None:
            effective_tier = getattr(spec.role_type_defaults, stage.role.type.value, None)
        if effective_tier and effective_tier in _LOW_TIER_NAMES and effective_tier in defined_tiers:
            errors.append(SpecError(
                code="GUIDED_JSON_LOW_TIER_RISK",
                message=(
                    f"Stage '{stage.id}' uses output_mode='guided_json' with tier '{effective_tier}'. "
                    f"Small/tiny models frequently produce schema-invalid JSON, forcing tier "
                    f"escalation and added latency. Consider 'medium' or higher for guided_json stages."
                ),
                stage_id=stage.id,
                severity="warning",
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

    # Keys the harness always injects into context at runtime — never flag these.
    harness_injected_keys: set[str] = set(runtime_context_keys(spec))

    # ── Memory pyramid: navigation requires memory.enabled ──
    if spec.memory and not spec.memory.enabled and spec.memory.navigation_tools:
        errors.append(SpecError(
            code="MEMORY_NAV_TOOLS_REQUIRES_ENABLED",
            message=(
                "memory.navigation_tools is True but memory.enabled is False; "
                "navigation tools will not be registered. Set enabled: true or "
                "navigation_tools: false."
            ),
            severity="warning",
        ))

    # ── Memory pyramid (Phase 2): memory-config misconfig warnings ──
    if spec.memory and spec.memory.enabled:
        if spec.memory.navigation_tools and not spec.memory.extract_knowledge:
            errors.append(SpecError(
                code="MEMORY_NAV_TOOLS_REQUIRES_EXTRACT",
                message=(
                    "memory.navigation_tools is True but extract_knowledge is False; "
                    "the L1 knowledge store will be empty so memory.search_records / "
                    "memory.get_records return nothing. Set extract_knowledge: true "
                    "or treat navigation as L0-only (memory.search_conversation / "
                    "memory.get_run_trace)."
                ),
                severity="warning",
            ))
        if spec.memory.reconcile_llm and not spec.memory.extract_knowledge:
            errors.append(SpecError(
                code="MEMORY_RECONCILE_LLM_WITHOUT_EXTRACT",
                message=(
                    "memory.reconcile_llm is True but extract_knowledge is False; "
                    "the reconciler only runs during extraction, so the LLM "
                    "tie-breaker never fires. Set extract_knowledge: true or "
                    "reconcile_llm: false."
                ),
                severity="warning",
            ))

    # Warn when a post_run stage has no signature filter and the workflow has fan_out
    # stages — the full _transcript will be enormous and will likely overflow context.
    has_fan_out = any(s.fan_out for s in spec.stages if not s.post_run)
    for stage in spec.stages:
        # Post_run stage with no signature.input and a fan_out workflow → transcript overflow risk.
        if stage.post_run and has_fan_out and (stage.signature is None or not stage.signature.input):
            errors.append(SpecError(
                code="POST_RUN_TRANSCRIPT_OVERFLOW_RISK",
                message=(
                    f"Stage '{stage.id}' is a post_run stage with no signature.input filter. "
                    f"This workflow has fan_out stages, so _transcript will be very large "
                    f"and may exceed the model's context limit. "
                    f"Add 'signature.input' to select only the outputs this stage needs."
                ),
                stage_id=stage.id,
                severity="warning",
            ))

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
                    or key in harness_injected_keys     # key is injected by the harness at runtime
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

    # ── loop IterationConfig validation ───────────────────────────────────────
    _RESERVED_CONTEXT_KEYS = frozenset({
        "_retry_attempt", "_last_result", "_last_error",
        "run_id", "_transcript", "_diagnostics", "_stale_memory_keys",
    })
    for stage in spec.stages:
        if stage.loop is None:
            continue
        lc = stage.loop
        if lc.max_iterations < 1:
            errors.append(SpecError(
                code="LOOP_INVALID_MAX_ITERATIONS",
                message=f"loop.max_iterations must be >= 1, got {lc.max_iterations}",
                stage_id=stage.id,
            ))
        if lc.until is not None:
            try:
                from jinja2 import Environment, BaseLoader
                Environment(loader=BaseLoader()).parse(lc.until)
            except Exception as exc:
                errors.append(SpecError(
                    code="LOOP_INVALID_UNTIL_EXPR",
                    message=f"loop.until is not a valid Jinja2 expression: {exc}",
                    stage_id=stage.id,
                ))
        if lc.carry_forward is not None:
            for path in lc.carry_forward:
                if not path or not path.strip():
                    errors.append(SpecError(
                        code="LOOP_EMPTY_CARRY_FORWARD_PATH",
                        message="loop.carry_forward entries must be non-empty strings",
                        stage_id=stage.id,
                    ))
        if not lc.iteration_var.isidentifier():
            errors.append(SpecError(
                code="LOOP_INVALID_ITERATION_VAR",
                message=(
                    f"loop.iteration_var '{lc.iteration_var}' is not a valid Python identifier"
                ),
                stage_id=stage.id,
            ))
        elif lc.iteration_var in _RESERVED_CONTEXT_KEYS:
            errors.append(SpecError(
                code="LOOP_RESERVED_ITERATION_VAR",
                message=(
                    f"loop.iteration_var '{lc.iteration_var}' conflicts with a reserved context key"
                ),
                stage_id=stage.id,
            ))
        if stage.on_fail is not None and stage.on_fail.loop is not None:
            errors.append(SpecError(
                code="LOOP_AND_ON_FAIL_LOOP_COEXIST",
                message=(
                    "Stage has both loop (deliberate iteration) and on_fail.loop (retry). "
                    "Both will execute: on_fail.loop handles failures within each iteration, "
                    "loop controls the outer iteration. Verify this is intentional."
                ),
                stage_id=stage.id,
                severity="warning",
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

    # ── Adapter factory validation ───────────────────────────────────────────
    _KNOWN_ADAPTER_BACKENDS = frozenset({
        "modal", "local", "together", "runpod", "replicate", "mock",
    })
    if spec.adapter_factory is not None:
        factory = spec.adapter_factory
        if factory.backend not in _KNOWN_ADAPTER_BACKENDS:
            errors.append(SpecError(
                code="UNKNOWN_ADAPTER_BACKEND",
                message=(
                    f"adapter_factory.backend '{factory.backend}' is not recognized; "
                    f"supported backends: {sorted(_KNOWN_ADAPTER_BACKENDS)}"
                ),
                stage_id=None,
            ))

        if factory.base_model is None:
            # If no base_model is declared, at least one tier must name the same
            # model implicitly. We can't verify that statically, so warn.
            errors.append(SpecError(
                code="ADAPTER_FACTORY_NO_BASE_MODEL",
                message=(
                    "adapter_factory has no base_model; adapters must be trained "
                    "against a base model matching one of the configured model tiers"
                ),
                stage_id=None,
                severity="warning",
            ))

    # Collect configured base models from tiers (including extras).
    configured_base_models: set[str] = set()
    for tier_name in ["tiny", "small", "medium", "large", "frontier"] + list(
        getattr(spec.model_tiers, "__pydantic_extra__", {}) or {}
    ):
        tier_cfg = getattr(spec.model_tiers, tier_name, None)
        if tier_cfg is not None and tier_cfg.model:
            configured_base_models.add(tier_cfg.model)

    for skill_id, skill_def in spec.skill_library.items():
        if skill_def.adapter is None:
            continue
        ref = skill_def.adapter
        if ref.fallback == "text" and not (skill_def.content or skill_def.path):
            errors.append(SpecError(
                code="ADAPTER_NO_FALLBACK",
                message=(
                    f"Skill '{skill_id}' has adapter fallback='text' but no "
                    f"content/path is provided for text fallback"
                ),
                stage_id=None,
            ))

        if spec.adapter_factory is not None and spec.adapter_factory.base_model:
            base = spec.adapter_factory.base_model
            if configured_base_models and base not in configured_base_models:
                errors.append(SpecError(
                    code="ADAPTER_BASE_MODEL_MISMATCH",
                    message=(
                        f"Skill '{skill_id}' adapter base model '{base}' does not "
                        f"match any configured model tier model"
                    ),
                    stage_id=None,
                ))

    # ── Context governance ───────────────────────────────────────────────
    layer_names = {l.name for l in spec.context_layers}
    if MISSION_LAYER_NAME in layer_names:
        errors.append(SpecError(
            code="RESERVED_CONTEXT_LAYER_NAME",
            message=(
                "context_layers contains a layer named 'mission', which is "
                "reserved for the auto layer synthesized from the top-level "
                "'mission' field"
            ),
        ))
    all_layer_names = layer_names | ({MISSION_LAYER_NAME} if spec.mission else set())
    never_targets = (stage_ids | workflow_input_keys | all_layer_names
                     | set(runtime_context_keys(spec)))

    for layer in spec.context_layers:
        for n in layer.never:
            if n not in never_targets:
                errors.append(SpecError(
                    code="UNKNOWN_CONTEXT_SOURCE",
                    message=(
                        f"context_layers '{layer.name}' never references '{n}', "
                        f"which is neither a stage id, runtime input, layer "
                        f"name, nor harness-injected key"
                    ),
                ))

    floor = floor_never(spec)

    def _check_policy(policy, where: str, stage_id: str | None,
                      applicable_never: set[str]) -> None:
        for m in policy.must:
            if m not in all_layer_names:
                errors.append(SpecError(
                    code="UNKNOWN_CONTEXT_LAYER",
                    message=f"{where}: must references unknown layer '{m}'",
                    stage_id=stage_id,
                ))
            if m in applicable_never:
                errors.append(SpecError(
                    code="CONTEXT_POLICY_CONTRADICTS_FLOOR",
                    message=(
                        f"{where}: must forces '{m}' but a never closes it — "
                        f"a closure always wins over a force"
                    ),
                    stage_id=stage_id,
                ))
        for n in policy.never:
            if n not in never_targets:
                errors.append(SpecError(
                    code="UNKNOWN_CONTEXT_SOURCE",
                    message=(
                        f"{where}: never references '{n}', which is neither a "
                        f"stage id, runtime input, layer name, nor "
                        f"harness-injected key"
                    ),
                    stage_id=stage_id,
                ))
        for m in sorted(set(policy.must) & set(policy.never)):
            errors.append(SpecError(
                code="CONTEXT_POLICY_CONTRADICTS_FLOOR",
                message=f"{where}: '{m}' is both must'd and never'd",
                stage_id=stage_id,
            ))

    if spec.context_policy is not None:
        _check_policy(spec.context_policy, "workflow context_policy", None,
                      floor | set(spec.context_policy.never))
    for stage in spec.stages:
        if stage.context_policy is None:
            continue
        applicable = set(floor)
        if spec.context_policy is not None:
            applicable.update(spec.context_policy.never)
        applicable.update(stage.context_policy.never)
        _check_policy(stage.context_policy, f"stage '{stage.id}'", stage.id, applicable)

        # partition_source resolves from the unfiltered context (rendered
        # pre-filter in the engine), so closing it is ineffective for the items.
        if stage.fan_out and stage.partition_source:
            m = _PARTITION_VAR_RE.match(stage.partition_source)
            if m and m.group(1) in stage.context_policy.never:
                errors.append(SpecError(
                    code="NEVER_BLOCKS_PARTITION_SOURCE",
                    message=(
                        f"stage '{stage.id}' never's '{m.group(1)}', which its "
                        f"partition_source reads — partition_source resolves "
                        f"from the unfiltered context, so the closure is "
                        f"ineffective for the partitioned items"
                    ),
                    stage_id=stage.id,
                    severity="warning",
                ))

        # A closed stage's content can still flow transitively through a
        # visible intermediate that read it — warn, cannot prevent.
        closed_stages = set(stage.context_policy.never) & stage_ids
        if closed_stages:
            readers = {s.id for s in spec.stages
                       if any(d in closed_stages for d in s.depends_on)}
            for y in sorted(set(stage.depends_on) & readers):
                errors.append(SpecError(
                    code="CONTEXT_TRANSIT_LEAK_RISK",
                    message=(
                        f"stage '{stage.id}' closes "
                        f"'{', '.join(sorted(closed_stages))}', but depends on "
                        f"'{y}' which reads that stage — the closed content "
                        f"may flow transitively through '{y}'s output"
                    ),
                    stage_id=stage.id,
                    severity="warning",
                ))

    if strict:
        hard_errors = [e for e in errors if e.severity != "warning"]
        if hard_errors:
            raise SpecValidationError(hard_errors)

    return errors
