"""Tests for the TypeSafe decision adapter spike (examples/decision-typesafe/decide.py).

All HTTP is mocked with httpx.MockTransport — no network, no key needed.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DECIDE = _REPO_ROOT / "examples" / "decision-typesafe" / "decide.py"


def _load_decide():
    spec = importlib.util.spec_from_file_location("decide", _DECIDE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def decide():
    return _load_decide()


QUESTIONS = {
    "is_concrete": {
        "type": "noul",
        "instructions": "Does the statement name a specific file, function, or observable symptom?",
        "criteria": {"true": "A specific artifact or behavior is named", "false": "The statement is vague"},
    },
    "severity": {
        "type": "choice",
        "instructions": "How severe is the issue described?",
        "criteria": {
            "low": "Cosmetic or stylistic",
            "medium": "Correctness risk in a narrow case",
            "high": "Data loss, security, or broad breakage",
        },
    },
    "actionability": {
        "type": "score",
        "instructions": "How directly can a developer act on this?",
        "criteria": ["vague", "partially actionable", "immediately actionable"],
    },
}


def _ok_response() -> dict:
    # Shape captured from the live API (2026-09-21): noul truth under 'noul',
    # each answer echoes its question type, score legends/probabilities are
    # dicts keyed by stringified level index.
    return {
        "model": "jev-1.13.0",
        "answers": {
            "is_concrete": {"type": "noul", "noul": 0.93},
            "severity": {"type": "choice", "choice": "high", "confidence": 0.88,
                         "probabilities": {"high": 0.88, "medium": 0.1, "low": 0.02}},
            "actionability": {"type": "score", "score": 2.1, "confidence": 0.9,
                              "legend": {"0": "vague", "1": "partially actionable", "2": "immediately actionable"},
                              "probabilities": {"0": 0.02, "1": 0.08, "2": 0.9}},
        },
        "usage": {"input_tokens": 412, "output_tokens": 96},
    }


def _patch_transport(monkeypatch, handler, decide):
    """Swap decide.build_client for one backed by a MockTransport."""
    def fake_build_client(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(decide, "build_client", fake_build_client)


class TestRequestShape:
    def test_posts_state_questions_model_to_systemone(self, decide, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_ok_response())

        _patch_transport(monkeypatch, handler, decide)
        result = decide.run_decision(
            state="resize_images.py exits 1 when the input file has zero bytes",
            questions=QUESTIONS,
            api_key="test-key",
        )

        assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
        assert captured["auth"] == "Bearer test-key"
        assert captured["body"]["model"] == "jev-latest"
        assert captured["body"]["state"] == "resize_images.py exits 1 when the input file has zero bytes"
        assert captured["body"]["questions"] == QUESTIONS
        assert result["answers"]["is_concrete"]["noul"] == 0.93
        assert result["usage"]["input_tokens"] == 412

    def test_result_carries_latency_ms(self, decide, monkeypatch):
        _patch_transport(monkeypatch, lambda req: httpx.Response(200, json=_ok_response()), decide)
        result = decide.run_decision(state="x", questions=QUESTIONS, api_key="k")
        assert isinstance(result["latency_ms"], (int, float))
        assert result["latency_ms"] >= 0
        assert result["model"] == "jev-1.13.0"


class TestRetries:
    def test_retries_on_429_then_succeeds(self, decide, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 2:
                return httpx.Response(429, json={"detail": "rate limited"})
            return httpx.Response(200, json=_ok_response())

        monkeypatch.setattr(decide, "RETRY_BACKOFF_SECONDS", [0, 0, 0])  # no sleeping in tests
        _patch_transport(monkeypatch, handler, decide)
        result = decide.run_decision(state="x", questions=QUESTIONS, api_key="k")
        assert len(calls) == 2
        assert result["answers"]["is_concrete"]["noul"] == 0.93

    def test_retries_on_529_then_succeeds(self, decide, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 2:
                return httpx.Response(529, json={"detail": "overloaded"})
            return httpx.Response(200, json=_ok_response())

        monkeypatch.setattr(decide, "RETRY_BACKOFF_SECONDS", [0, 0, 0])
        _patch_transport(monkeypatch, handler, decide)
        decide.run_decision(state="x", questions=QUESTIONS, api_key="k")
        assert len(calls) == 2

    def test_gives_up_after_max_attempts(self, decide, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={"detail": "rate limited"})

        monkeypatch.setattr(decide, "RETRY_BACKOFF_SECONDS", [0, 0, 0])
        _patch_transport(monkeypatch, handler, decide)
        with pytest.raises(decide.DecisionError, match="429"):
            decide.run_decision(state="x", questions=QUESTIONS, api_key="k")
        assert len(calls) == 3

    def test_auth_error_is_not_retried(self, decide, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(401, json={"detail": "invalid api key"})

        _patch_transport(monkeypatch, handler, decide)
        with pytest.raises(decide.DecisionError, match="401"):
            decide.run_decision(state="x", questions=QUESTIONS, api_key="k")
        assert len(calls) == 1


class TestDryRun:
    def test_dry_run_returns_canned_answers_without_key(self, decide):
        result = decide.run_decision(
            state="anything",
            questions=QUESTIONS,
            api_key=None,
            dry_run=True,
        )
        assert set(result["answers"]) == set(QUESTIONS)
        assert result["dry_run"] is True
        assert result["latency_ms"] == 0
        assert "usage" in result

    def test_dry_run_answers_match_question_types(self, decide):
        result = decide.run_decision(state="s", questions=QUESTIONS, api_key=None, dry_run=True)
        assert "noul" in result["answers"]["is_concrete"]
        assert "choice" in result["answers"]["severity"]
        assert "score" in result["answers"]["actionability"]


class TestContextInput:
    def test_reads_state_from_armature_context(self, decide, monkeypatch):
        monkeypatch.setenv("ARMATURE_CONTEXT", json.dumps({
            "state_item": {"id": "s1", "text": "api.py:44 drops the Authorization header on redirect"}
        }))
        monkeypatch.setattr(decide, "STATE_FALLBACK", "")
        state = decide.resolve_state(None)
        assert state == "api.py:44 drops the Authorization header on redirect"

    def test_cli_state_flag_wins(self, decide, monkeypatch):
        monkeypatch.setenv("ARMATURE_CONTEXT", json.dumps({"state_item": {"text": "from context"}}))
        assert decide.resolve_state("from flag") == "from flag"

    def test_missing_state_raises(self, decide, monkeypatch):
        monkeypatch.delenv("ARMATURE_CONTEXT", raising=False)
        monkeypatch.setattr(decide, "STATE_FALLBACK", "")
        with pytest.raises(decide.DecisionError, match="[Ss]tate"):
            decide.resolve_state(None)
