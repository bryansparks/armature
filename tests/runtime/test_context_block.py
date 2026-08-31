"""Unit tests for the _build_context_block() engine helper."""
from armature.runtime.engine import _build_context_block


def test_empty_everything_returns_empty():
    assert _build_context_block([], context={}, spec_stage_ids=set()) == ""


def test_mission_layer_renders_workflow_mission_header():
    result = _build_context_block(
        [("[Workflow Mission]", "Produce a Q3 market report.")],
        context={}, spec_stage_ids=set(),
    )
    assert result == "[Workflow Mission]\nProduce a Q3 market report."


def test_named_layer_renders_context_layer_header():
    result = _build_context_block(
        [("[Context Layer: principles]", "Be terse.")],
        context={}, spec_stage_ids=set(),
    )
    assert result == "[Context Layer: principles]\nBe terse."


def test_layers_render_in_order_then_breadcrumbs():
    result = _build_context_block(
        [("[Workflow Mission]", "m"), ("[Context Layer: p]", "ppp")],
        context={"gather": {"count": 5}},
        spec_stage_ids={"gather"},
    )
    assert result.startswith("[Workflow Mission]\nm\n\n[Context Layer: p]\nppp")
    assert "[Prior stages]" in result
    assert "• gather →" in result


def test_breadcrumbs_only_show_keys_in_context():
    result = _build_context_block(
        [], context={"stage_a": {"answer": "yes"}},
        spec_stage_ids={"stage_a", "stage_b"},
    )
    assert "• stage_a →" in result
    assert "stage_b" not in result


def test_truncates_long_output():
    long_val = "x" * 500
    result = _build_context_block(
        [], context={"big": {"data": long_val}}, spec_stage_ids={"big"},
    )
    line = [l for l in result.splitlines() if "• big →" in l][0]
    assert len(line) <= len("• big → ") + 200


def test_blank_layer_content_is_skipped():
    result = _build_context_block(
        [("[Context Layer: empty]", "   ")], context={}, spec_stage_ids=set(),
    )
    assert result == ""
