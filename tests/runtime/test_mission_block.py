"""Unit tests for the _build_mission_block() engine helper."""
import json
import pytest
from armature.runtime.engine import _build_mission_block


def test_build_mission_block_empty_everything_returns_empty():
    result = _build_mission_block(mission="", context={}, spec_stage_ids=set())
    assert result == ""


def test_build_mission_block_mission_only():
    result = _build_mission_block(
        mission="Produce a Q3 market report.",
        context={},
        spec_stage_ids=set(),
    )
    assert result == "[Workflow Mission]\nProduce a Q3 market report."


def test_build_mission_block_no_mission_with_prior_stages():
    result = _build_mission_block(
        mission="",
        context={"gather": {"count": 5}},
        spec_stage_ids={"gather"},
    )
    assert "[Prior stages]" in result
    assert "• gather →" in result
    assert "[Workflow Mission]" not in result


def test_build_mission_block_mission_and_prior_stages():
    result = _build_mission_block(
        mission="Deliver a report.",
        context={"stage_a": {"answer": "yes"}},
        spec_stage_ids={"stage_a", "stage_b"},
    )
    assert result.startswith("[Workflow Mission]")
    assert "Deliver a report." in result
    assert "• stage_a →" in result
    assert "stage_b" not in result  # not in context yet — not completed


def test_build_mission_block_truncates_long_output():
    long_val = "x" * 500
    result = _build_mission_block(
        mission="",
        context={"big": {"data": long_val}},
        spec_stage_ids={"big"},
        max_preview_chars=200,
    )
    # The preview of the JSON-serialized value should not exceed 200 chars
    line = [l for l in result.splitlines() if "• big →" in l][0]
    assert len(line) <= len("• big → ") + 200
