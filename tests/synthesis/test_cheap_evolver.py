"""Tests confirming SpecRefiner uses medium-tier (not frontier) model by default.

Based on arXiv:2605.30621v1: frontier models are not needed for spec evolution;
medium-tier models achieve equivalent results at significantly lower cost.
"""
import pytest
from armature.synthesis.improve import SpecRefiner, SelfImproveRunner


def test_self_improve_runner_default_model_is_not_frontier(tmp_path):
    """SelfImproveRunner must resolve a non-frontier (non-Opus) model by default.

    Per arXiv:2605.30621v1, medium-tier models suffice for spec evolution.
    When the spec has no model tiers, the resolver falls back to claude-sonnet-4-6,
    not the frontier Opus model.
    """
    from armature.synthesis.improve import _resolve_refiner_model
    from armature.spec.loader import load_spec

    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: test-wf\nversion: '1.0'\nstages:\n  - id: s1\n    role:\n      name: W\n      type: worker\n      description: do it\n"
    )
    spec = load_spec(spec_file)
    resolved = _resolve_refiner_model(spec)
    assert "opus" not in resolved.lower(), (
        f"Resolved model '{resolved}' appears to be frontier (Opus); "
        "per arXiv:2605.30621v1, medium-tier suffices for spec evolution"
    )


def test_spec_refiner_stores_configured_model():
    """SpecRefiner stores the model string and uses it for LLM calls."""
    refiner = SpecRefiner("claude-sonnet-4-6")
    assert refiner._model == "claude-sonnet-4-6"
