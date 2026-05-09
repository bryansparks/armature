import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from armature.skills.alembic import submit_trace, register_alembic_hook
from armature.state.traces import TraceRecord
from armature.hooks.lifecycle import HookRegistry, HookPhase


def _make_trace(**kwargs) -> dict:
    defaults = dict(
        run_id="r1", workflow_name="w", stage_id="s",
        role_type="worker", model="qwen", latency_ms=100,
        success=True, output_valid=True,
    )
    defaults.update(kwargs)
    return TraceRecord(**defaults).model_dump()


def _mock_client(trace_id: str = "abc123"):
    """Return a context manager mock that fakes httpx.AsyncClient.post."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"trace_id": trace_id}
    mock_response.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, mock_client


async def test_submit_trace_calls_api():
    cm, mock_client = _mock_client("abc123")
    with patch("httpx.AsyncClient", return_value=cm):
        result = await submit_trace({
            "trace": _make_trace(quorum_score=0.9),
            "score": 0.9,
            "alembic_url": "http://localhost:8001",
        })
    assert result["submitted"] is True
    assert result["trace_id"] == "abc123"


async def test_submit_trace_with_default_url():
    cm, mock_client = _mock_client("xyz")
    with patch("httpx.AsyncClient", return_value=cm):
        result = await submit_trace({"trace": _make_trace()})
    assert result["submitted"] is True
    call_url = mock_client.post.call_args[0][0]
    assert "localhost:8001" in call_url


async def test_submit_trace_uses_custom_url():
    cm, mock_client = _mock_client()
    with patch("httpx.AsyncClient", return_value=cm):
        await submit_trace({
            "trace": _make_trace(),
            "alembic_url": "http://alembic.internal:9000",
        })
    call_url = mock_client.post.call_args[0][0]
    assert "alembic.internal:9000" in call_url


async def test_submit_trace_raises_on_http_error():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    mock_client.post = AsyncMock(return_value=mock_response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=cm):
        with pytest.raises(httpx.HTTPStatusError):
            await submit_trace({"trace": _make_trace()})


def test_register_alembic_hook_adds_post_stage_hook():
    registry = HookRegistry()
    register_alembic_hook(registry, threshold=0.8)
    assert len(registry._hooks[HookPhase.POST_STAGE]) == 1


async def test_alembic_hook_submits_when_score_meets_threshold():
    """POST_STAGE hook submits when _quorum_score >= threshold."""
    registry = HookRegistry()
    submitted = []

    async def fake_submit(args):
        submitted.append(args["score"])
        return {"submitted": True, "trace_id": "t1"}

    with patch("armature.skills.alembic.submit_trace", side_effect=fake_submit):
        register_alembic_hook(registry, threshold=0.8)
        await registry.run_post_stage("s1", {"output": "ok"}, {"_quorum_score": 0.9})

    assert submitted == [0.9]


async def test_alembic_hook_skips_when_score_below_threshold():
    """POST_STAGE hook does not submit when _quorum_score < threshold."""
    registry = HookRegistry()
    submitted = []

    async def fake_submit(args):
        submitted.append(True)
        return {"submitted": True, "trace_id": "t1"}

    with patch("armature.skills.alembic.submit_trace", side_effect=fake_submit):
        register_alembic_hook(registry, threshold=0.8)
        await registry.run_post_stage("s1", {"output": "ok"}, {"_quorum_score": 0.7})

    assert submitted == []


async def test_alembic_hook_skips_when_no_quorum_score():
    """POST_STAGE hook does not submit when _quorum_score is absent from ctx."""
    registry = HookRegistry()
    submitted = []

    async def fake_submit(args):
        submitted.append(True)
        return {"submitted": True, "trace_id": "t1"}

    with patch("armature.skills.alembic.submit_trace", side_effect=fake_submit):
        register_alembic_hook(registry, threshold=0.8)
        await registry.run_post_stage("s1", {"output": "ok"}, {})

    assert submitted == []


async def test_alembic_hook_does_not_raise_on_submission_error():
    """Alembic submission errors are silently swallowed to not block execution."""
    registry = HookRegistry()

    async def always_fail(args):
        raise RuntimeError("alembic down")

    with patch("armature.skills.alembic.submit_trace", side_effect=always_fail):
        register_alembic_hook(registry, threshold=0.5)
        # Should not raise
        await registry.run_post_stage("s1", {}, {"_quorum_score": 0.9})
