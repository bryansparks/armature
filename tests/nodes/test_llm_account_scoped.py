import asyncio
import pytest
import litellm
from armature.nodes import llm


class _CreditsErr(litellm.APIError):
    """A 402 insufficient-credits error that IS a litellm.APIError (hence
    retryable under the old _RETRYABLE_ERRORS tuple). The fix must detect it
    as account-scoped and raise on the first attempt with no backoff sleep."""

    def __init__(self):
        super().__init__(
            status_code=402,
            message="insufficient credits",
            model="m",
            llm_provider="openrouter",
        )


def test_account_scoped_error_not_retried(monkeypatch):
    """A 402 is account-scoped: _call_with_retry raises on the first attempt
    and never sleeps (no backoff)."""
    calls = {"n": 0}

    async def boom(**kw):
        calls["n"] += 1
        raise _CreditsErr()

    monkeypatch.setattr(llm, "litellm_completion", boom)
    slept = []
    async def fake_sleep(d):
        slept.append(d)
    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)

    with pytest.raises(_CreditsErr):
        asyncio.run(llm._call_with_retry(model="m", max_retries=5, messages=[]))
    assert calls["n"] == 1
    assert slept == []


def test_transient_error_is_retried(monkeypatch):
    """A retryable error still retries with backoff (regression guard)."""
    calls = {"n": 0}

    async def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("transient")
        return "ok-response"

    monkeypatch.setattr(llm, "litellm_completion", flaky)
    monkeypatch.setattr(llm, "_RETRYABLE_ERRORS", (ValueError,))
    slept = []
    async def fake_sleep(d):
        slept.append(d)
    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)

    res = asyncio.run(llm._call_with_retry(model="m", max_retries=5, messages=[]))
    assert res == "ok-response"
    assert calls["n"] == 2
    assert len(slept) == 1
