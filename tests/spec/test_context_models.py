"""Tests for ContextLayer / ContextPolicy spec models."""
import pytest
from pydantic import ValidationError

from armature.spec.models import ContextLayer, ContextPolicy, HarnessSpec


def test_context_layer_inline_content():
    layer = ContextLayer(name="principles", content="Be terse.")
    assert layer.name == "principles"
    assert layer.content == "Be terse."
    assert layer.src is None
    assert layer.precedence == 0
    assert layer.never == []


def test_context_layer_src_only():
    layer = ContextLayer(name="domain", src="domain.md", precedence=10)
    assert layer.src == "domain.md"
    assert layer.content is None
    assert layer.precedence == 10


def test_context_layer_requires_exactly_one_source():
    with pytest.raises(ValidationError):
        ContextLayer(name="bad")  # neither
    with pytest.raises(ValidationError):
        ContextLayer(name="bad", content="x", src="y")  # both


def test_context_policy_defaults():
    policy = ContextPolicy()
    assert policy.must == []
    assert policy.never == []


def test_spec_accepts_context_sections():
    spec = HarnessSpec.model_validate({
        "name": "wf",
        "stages": [
            {"id": "a", "context_policy": {"never": ["b"]}},
            {"id": "b"},
        ],
        "context_layers": [{"name": "principles", "content": "Be terse."}],
        "context_policy": {"must": ["principles"]},
    })
    assert spec.context_layers[0].name == "principles"
    assert spec.context_policy.must == ["principles"]
    assert spec.stages[0].context_policy.never == ["b"]
    assert spec.stages[1].context_policy is None
