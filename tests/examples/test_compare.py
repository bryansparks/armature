"""Tests for the A/B comparison script (examples/decision-typesafe/compare.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPARE = _REPO_ROOT / "examples" / "decision-typesafe" / "compare.py"


@pytest.fixture
def compare():
    spec = importlib.util.spec_from_file_location("compare", _COMPARE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATES = [
    {"id": "s1", "text": "api.py:44 leaks the auth header on redirect", "labels": {"is_concrete": True, "severity": "high", "actionability": 2}},
    {"id": "s2", "text": "code quality could be better", "labels": {"is_concrete": False, "severity": "low", "actionability": 0}},
    {"id": "s3", "text": "db.py:112 builds SQL by string formatting", "labels": {"is_concrete": True, "severity": "high", "actionability": 2}},
    {"id": "s4", "text": "error handling is inconsistent", "labels": {"is_concrete": False, "severity": "low", "actionability": 0}},
]

DECIDE = [
    {"model": "jev-1.13.0", "answers": {"is_concrete": {"truth": 0.95}, "severity": {"choice": "high"}, "actionability": {"score": 1.8}}, "usage": {"input_tokens": 500, "output_tokens": 0}, "latency_ms": 110.0},
    {"model": "jev-1.13.0", "answers": {"is_concrete": {"truth": 0.2}, "severity": {"choice": "low"}, "actionability": {"score": 0.4}}, "usage": {"input_tokens": 480, "output_tokens": 0}, "latency_ms": 105.0},
    {"model": "jev-1.13.0", "answers": {"is_concrete": {"truth": 0.9}, "severity": {"choice": "medium"}, "actionability": {"score": 2.2}}, "usage": {"input_tokens": 520, "output_tokens": 0}, "latency_ms": 120.0},
    {"_fan_out_error": "TypeSafe API returned 500", "vulnerabilities": []},
]

JUDGE = [
    {"concrete": True, "severity": "high", "actionability": 2},
    {"concrete": False, "severity": "low", "actionability": 0},
    {"concrete": True, "severity": "high", "actionability": 1},
    {"concrete": False, "severity": "medium", "actionability": 1},
]


class TestDimensionScoring:
    def test_noul_thresholds(self, compare):
        # live API shape: truth under 'noul'
        assert compare.noul_correct({"noul": 0.6}, True)
        assert compare.noul_correct({"noul": 0.4}, False)
        assert not compare.noul_correct({"noul": 0.6}, False)

    def test_noul_accepts_truth_fallback(self, compare):
        # older mocked shape: truth under 'truth'
        assert compare.noul_correct({"truth": 0.6}, True)
        assert compare.noul_correct({"truth": 0.4}, False)

    def test_choice_exact(self, compare):
        assert compare.choice_correct({"choice": "high"}, "high")
        assert not compare.choice_correct({"choice": "medium"}, "high")

    def test_score_rounds(self, compare):
        assert compare.score_correct({"score": 1.8}, 2)
        assert compare.score_correct({"score": 1.4}, 1)
        assert not compare.score_correct({"score": 0.9}, 2)


class TestSummarize:
    def test_counts_and_alignment(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        # 3 usable pairs (one decide failure), index-aligned
        assert summary["pairs"] == 3
        assert summary["decision_failures"] == 1
        assert summary["judge_failures"] == 0

    def test_decision_accuracy(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        # s1 all correct, s2 all correct, s3: concrete yes, severity medium vs high (miss), actionability 2 vs 2 (hit)
        d = summary["decision"]
        assert d["is_concrete"] == pytest.approx(3 / 3)
        assert d["severity"] == pytest.approx(2 / 3)
        assert d["actionability"] == pytest.approx(3 / 3)

    def test_judge_accuracy(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        # s1 all correct, s2 all correct, s3: concrete yes, severity high (hit), actionability 1 vs 2 (miss)
        j = summary["judge"]
        assert j["is_concrete"] == pytest.approx(3 / 3)
        assert j["severity"] == pytest.approx(3 / 3)
        assert j["actionability"] == pytest.approx(2 / 3)

    def test_agreement_between_systems(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        a = summary["agreement"]
        # s1: full agree; s2: full agree; s3: concrete agree, severity disagree, actionability disagree
        assert a["is_concrete"] == pytest.approx(3 / 3)
        assert a["severity"] == pytest.approx(2 / 3)
        assert a["actionability"] == pytest.approx(2 / 3)

    def test_latency_and_cost(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        # summarize rounds latency to 0.1 ms
        assert summary["decision_latency_ms"]["mean"] == pytest.approx((110 + 105 + 120) / 3, abs=0.1)
        assert summary["decision_latency_ms"]["p50"] == pytest.approx(110)
        # 1500 input tokens * $42/Btok
        assert summary["decision_cost_usd"] == pytest.approx(1500 * 42 / 1e9)

    def test_overall_accuracy(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        # decision: 8 of 9 dimension-correct; judge: 8 of 9
        assert summary["decision"]["overall"] == pytest.approx(8 / 9)
        assert summary["judge"]["overall"] == pytest.approx(8 / 9)

    def test_per_state_detail(self, compare):
        summary = compare.summarize(STATES, DECIDE, JUDGE)
        per_state = summary["per_state"]
        assert len(per_state) == 4
        # failed item recorded as failed, no detail
        assert per_state[3] == {"id": "s4", "failed": True}
        # s1: decision matches label everywhere, no missed dims
        assert per_state[0]["missed"] == []
        assert per_state[0]["decision"] == {"is_concrete": True, "severity": "high", "actionability": 2}
        assert per_state[0]["judge"] == {"is_concrete": True, "severity": "high", "actionability": 2}
        # s3: severity missed by decision (medium vs high)
        assert "severity" in per_state[2]["missed"]
        assert "actionability" not in per_state[2]["missed"]
        assert per_state[2]["label"]["severity"] == "high"
