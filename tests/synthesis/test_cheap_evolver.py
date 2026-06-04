"""Tests confirming SpecRefiner uses medium-tier (not frontier) model by default.

Based on arXiv:2605.30621v1: frontier models are not needed for spec evolution;
medium-tier models achieve equivalent results at significantly lower cost.
"""
import pytest
from armature.synthesis.improve import SpecRefiner, SelfImproveRunner


def test_self_improve_runner_default_model_is_not_frontier(tmp_path):
    """SelfImproveRunner's default model must NOT be the frontier (Opus) model."""
    spec_file = tmp_path / "wf.yaml"
    spec_file.write_text(
        "name: test-wf\nversion: '1.0'\nstages:\n  - id: s1\n    role:\n      name: W\n      type: worker\n      description: do it\n"
    )
    runner = SelfImproveRunner(spec_file)
    assert "opus" not in runner._model.lower(), (
        f"Default model '{runner._model}' appears to be frontier (Opus); "
        "per arXiv:2605.30621v1, medium-tier suffices for spec evolution"
    )


def test_spec_refiner_stores_configured_model():
    """SpecRefiner stores the model string and uses it for LLM calls."""
    refiner = SpecRefiner("claude-sonnet-4-6")
    assert refiner._model == "claude-sonnet-4-6"
