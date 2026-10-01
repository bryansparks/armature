"""ClosureRecord + extract_closure: the inter-run contract as typed records (design §3)."""
import pytest

from armature.spec.models import CLOSURE_REASONS
from armature.state.closure import ClosureError, ClosureRecord, FollowOnUnit, extract_closure


def _spec_with_closure():
    from armature.spec.models import HarnessSpec
    # minimal spec: name + a closure pointing at a guided_json stage
    return HarnessSpec.model_validate({
        "name": "c-flow",
        "closure": {"stage": "final"},
        "stages": [{"id": "final", "depends_on": [],
                    "output_mode": "guided_json",
                    "output_schema": {"type": "object", "required": ["reason"],
                                      "properties": {"reason": {"type": "string"}}}}],
    })


def _spec_without_closure():
    from armature.spec.models import HarnessSpec
    return HarnessSpec.model_validate({
        "name": "plain-flow",
        "stages": [{"id": "final", "depends_on": [], "output_mode": "guided_json",
                    "output_schema": {"type": "object", "required": ["reason"],
                                      "properties": {"reason": {"type": "string"}}}}],
    })


def test_extract_returns_none_when_spec_has_no_closure():
    results = {"final": {"reason": "done_no_follow_on"}}
    assert extract_closure(_spec_without_closure(), results) is None


def test_extract_returns_none_when_stage_absent_from_results():
    assert extract_closure(_spec_with_closure(), {}) is None


def test_extracts_typed_record_and_filters_underscore_keys():
    results = {"final": {"reason": "handed_off", "notes": "spawned two",
                         "follow_on": [
                             {"id": "polish", "title": "Polish", "workflow": "wf.yml",
                              "inputs": {"a": 1}}],
                         "_input_tokens": 5, "_cost_usd": 0.01}}
    rec = extract_closure(_spec_with_closure(), results)
    assert rec.reason == "handed_off"
    assert rec.follow_on[0].id == "polish"
    assert rec.follow_on[0].inputs == {"a": 1}
    assert not hasattr(rec, "_input_tokens")          # engine internals filtered


def test_extract_rejects_reason_outside_the_closure_set():
    results = {"final": {"reason": "made_up_reason"}}
    with pytest.raises(ClosureError, match="reason"):
        extract_closure(_spec_with_closure(), results)


def test_extract_rejects_non_dict_stage_output():
    results = {"final": "some text"}
    with pytest.raises(ClosureError):
        extract_closure(_spec_with_closure(), results)


def test_closure_record_model_round_trips():
    rec = ClosureRecord(reason="blocked_on", follow_on=[
        FollowOnUnit(id="b", title="B", workflow="w")])
    dumped = rec.model_dump_json()
    assert ClosureRecord.model_validate_json(dumped).reason == "blocked_on"
    assert all(r in CLOSURE_REASONS for r in CLOSURE_REASONS)