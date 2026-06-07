"""Tests for cross-stage signature type-compatibility validation.

When stage B depends_on stage A and both have signature blocks,
the validator checks that shared keys have compatible types.
"""
import pytest
from armature.spec.models import (
    HarnessSpec, Stage, Signature, ToolCallConfig,
)
from armature.spec.validator import validate_spec


def _make_spec(stages: list[Stage]) -> HarnessSpec:
    return HarnessSpec(name="wf", stages=stages, validate=False)


def codes(errors) -> set[str]:
    return {e.code for e in errors}


# ── No mismatch — happy paths ─────────────────────────────────────────────────

def test_compatible_types_no_error():
    """A→B: A outputs str, B inputs str for same key → clean."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"result": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"result": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" not in codes(errors)


def test_no_shared_keys_no_error():
    """A→B: A outputs 'x', B inputs 'y' — no shared key, no mismatch."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "int"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"y": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" not in codes(errors)


def test_upstream_has_no_signature_no_error():
    """A→B: A has no signature — nothing to compare, no error."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t")),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"result": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" not in codes(errors)


def test_downstream_has_no_signature_no_error():
    """A→B: B has no signature — nothing to compare, no error."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"result": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"]),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" not in codes(errors)


def test_no_depends_on_no_error():
    """Two independent stages with conflicting-looking signatures — no edge, no check."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"brief": "ResearchBrief"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"),
              signature=Signature(input={"brief": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" not in codes(errors)


def test_output_superset_no_error():
    """A outputs {x, y, z}, B only inputs {x}. Subset is fine."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "str", "y": "int", "z": "bool"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"x": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" not in codes(errors)


# ── Type mismatch — error cases ───────────────────────────────────────────────

def test_type_mismatch_detected():
    """A outputs {result: str}, B inputs {result: ResearchBrief} → SIGNATURE_TYPE_MISMATCH."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"result": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"result": "ResearchBrief"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "SIGNATURE_TYPE_MISMATCH" in codes(errors)


def test_type_mismatch_error_names_stage():
    """Mismatch error includes the downstream stage id."""
    spec = _make_spec([
        Stage(id="research", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"brief": "str"})),
        Stage(id="judge", tool_call=ToolCallConfig(name="t"), depends_on=["research"],
              signature=Signature(input={"brief": "ResearchBrief"})),
    ])
    errors = validate_spec(spec, strict=False)
    mismatch = [e for e in errors if e.code == "SIGNATURE_TYPE_MISMATCH"]
    assert len(mismatch) == 1
    assert mismatch[0].stage_id == "judge"


def test_type_mismatch_error_message_names_key():
    """Mismatch error message mentions the conflicting key name."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"brief": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"brief": "ResearchBrief"})),
    ])
    errors = validate_spec(spec, strict=False)
    mismatch = next(e for e in errors if e.code == "SIGNATURE_TYPE_MISMATCH")
    assert "brief" in mismatch.message


def test_multiple_mismatched_keys_all_reported():
    """When two keys mismatch, both generate errors."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "str", "y": "int"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"x": "bool", "y": "float"})),
    ])
    errors = validate_spec(spec, strict=False)
    mismatches = [e for e in errors if e.code == "SIGNATURE_TYPE_MISMATCH"]
    assert len(mismatches) == 2


def test_type_mismatch_in_chain_a_b_c():
    """A→B→C: mismatch between B and C is detected; A→B is clean."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"x": "str"}, output={"summary": "str"})),
        Stage(id="c", tool_call=ToolCallConfig(name="t"), depends_on=["b"],
              signature=Signature(input={"summary": "Summary"})),
    ])
    errors = validate_spec(spec, strict=False)
    mismatches = [e for e in errors if e.code == "SIGNATURE_TYPE_MISMATCH"]
    assert len(mismatches) == 1
    assert mismatches[0].stage_id == "c"


def test_multiple_deps_one_mismatches():
    """B depends on both A1 and A2. A2's type conflicts — only that mismatch reported."""
    spec = _make_spec([
        Stage(id="a1", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "str"})),
        Stage(id="a2", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"y": "int"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a1", "a2"],
              signature=Signature(input={"x": "str", "y": "float"})),
    ])
    errors = validate_spec(spec, strict=False)
    mismatches = [e for e in errors if e.code == "SIGNATURE_TYPE_MISMATCH"]
    assert len(mismatches) == 1
    assert "y" in mismatches[0].message


# ── Undefined signature input ─────────────────────────────────────────────────

def test_input_key_not_in_any_upstream_output_flagged():
    """B inputs 'missing_key' but no upstream stage outputs it → UNDEFINED_SIGNATURE_INPUT."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"missing_key": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" in codes(errors)


def test_input_key_provided_by_contracts_inputs_not_flagged():
    """B inputs 'repo' — it's in contracts.inputs, so it's valid even if no stage outputs it."""
    from armature.spec.models import Contract
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="b", tool_call=ToolCallConfig(name="t"),
                  signature=Signature(input={"repo": "str"})),
        ],
        contracts=Contract(inputs=[{"name": "repo", "required": True}]),
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


def test_stage_with_no_depends_on_input_not_flagged():
    """A stage with no deps and signature.input — inputs come from workflow context, not stages."""
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(input={"query": "str"})),
    ])
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


def test_strict_true_raises_on_mismatch():
    """strict=True (default) raises SpecValidationError when mismatch found."""
    from armature.spec.validator import SpecValidationError
    spec = _make_spec([
        Stage(id="a", tool_call=ToolCallConfig(name="t"),
              signature=Signature(output={"x": "str"})),
        Stage(id="b", tool_call=ToolCallConfig(name="t"), depends_on=["a"],
              signature=Signature(input={"x": "int"})),
    ])
    with pytest.raises(SpecValidationError):
        validate_spec(spec, strict=True)


# ── Harness-injected context keys ─────────────────────────────────────────────

def test_run_id_is_always_valid_signature_input():
    """run_id is injected by the harness at runtime — never flag it as undefined."""
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="upstream", tool_call=ToolCallConfig(name="t"), depends_on=[]),
            Stage(id="a", tool_call=ToolCallConfig(name="t"), depends_on=["upstream"],
                  signature=Signature(input={"run_id": "str", "topic": "str"})),
        ],
        contracts={"inputs": [{"name": "topic"}]},
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


def test_continuation_inject_as_is_valid_signature_input():
    """Keys injected by continuation.inject_as are harness-injected — never flag them."""
    from armature.spec.models import ContinuationConfig, ContinuationKey
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="upstream", tool_call=ToolCallConfig(name="t"), depends_on=[]),
            Stage(id="a", tool_call=ToolCallConfig(name="t"), depends_on=["upstream"],
                  signature=Signature(input={"prior_research": "str"})),
        ],
        continuation=ContinuationConfig(
            carry_forward=[ContinuationKey(key="some_stage.some_key")],
            inject_as="prior_research",
        ),
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


def test_memory_inject_as_is_valid_signature_input():
    """Keys injected by memory.inject_as are harness-injected — never flag them."""
    from armature.spec.models import MemoryConfig, MemoryCapture
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="upstream", tool_call=ToolCallConfig(name="t"), depends_on=[]),
            Stage(id="a", tool_call=ToolCallConfig(name="t"), depends_on=["upstream"],
                  signature=Signature(input={"_prior_sources": "str"})),
        ],
        memory=MemoryConfig(
            enabled=True,
            capture=[MemoryCapture(stage="a", key="x")],
            inject_as="_prior_sources",
        ),
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


def test_transcript_and_diagnostics_valid_in_post_run_signature():
    """_transcript and _diagnostics are injected into post_run context — never flag them."""
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="a", tool_call=ToolCallConfig(name="t"), depends_on=[]),
            Stage(id="post", post_run=True, tool_call=ToolCallConfig(name="t"),
                  depends_on=["a"],
                  signature=Signature(input={"_transcript": "list", "_diagnostics": "list"})),
        ],
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    assert "UNDEFINED_SIGNATURE_INPUT" not in codes(errors)


# ── Post-run stage without signature warns when workflow has fan_out ───────────

def test_post_run_with_fan_out_and_no_signature_emits_warning():
    """A post_run stage with no signature.input gets the full _transcript,
    which grows large when the workflow has fan_out stages. Emit a WARNING."""
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="searcher", tool_call=ToolCallConfig(name="t"),
                  fan_out=10, partition_source="{{ items }}", depends_on=[],
                  partition_key="item"),
            Stage(id="analyst", post_run=True, tool_call=ToolCallConfig(name="t"),
                  depends_on=[]),
        ],
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if e.severity == "warning"}
    assert "POST_RUN_TRANSCRIPT_OVERFLOW_RISK" in warning_codes


def test_post_run_with_signature_no_warning_even_with_fan_out():
    """A post_run stage that declares signature.input filters its context — no warning needed."""
    from armature.spec.models import Signature
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="searcher", tool_call=ToolCallConfig(name="t"),
                  fan_out=10, partition_source="{{ items }}", depends_on=[],
                  partition_key="item"),
            Stage(id="analyst", post_run=True, tool_call=ToolCallConfig(name="t"),
                  depends_on=[],
                  signature=Signature(input={"searcher": "list"})),
        ],
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if hasattr(e, "severity") and e.severity == "warning"}
    assert "POST_RUN_TRANSCRIPT_OVERFLOW_RISK" not in warning_codes


def test_post_run_without_fan_out_no_warning():
    """A post_run stage with no fan_out in the workflow — transcript is small, no warning."""
    spec = HarnessSpec(
        name="wf",
        stages=[
            Stage(id="worker", tool_call=ToolCallConfig(name="t"), depends_on=[]),
            Stage(id="analyst", post_run=True, tool_call=ToolCallConfig(name="t"),
                  depends_on=[]),
        ],
        validate=False,
    )
    errors = validate_spec(spec, strict=False)
    warning_codes = {e.code for e in errors if hasattr(e, "severity") and e.severity == "warning"}
    assert "POST_RUN_TRANSCRIPT_OVERFLOW_RISK" not in warning_codes
