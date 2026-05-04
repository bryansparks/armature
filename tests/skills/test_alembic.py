import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from armature.skills.alembic import submit_trace, register_alembic_hook
from armature.state.traces import TraceRecord
from armature.hooks.lifecycle import HookRegistry, HookPhase


async def test_submit_trace_calls_api():
    trace = TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s",
        role_type="worker", model="qwen", latency_ms=100,
        success=True, output_valid=True, quorum_score=0.9,
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.json.return_value = {"trace_id": "abc123"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await submit_trace({
            "trace": trace.model_dump(),
            "score": 0.9,
            "alembic_url": "http://localhost:8001",
        })

    assert result["submitted"] is True
    assert result["trace_id"] == "abc123"


async def test_submit_trace_with_default_url():
    trace = TraceRecord(
        run_id="r1", workflow_name="w", stage_id="s",
        role_type="worker", model="qwen", latency_ms=100,
        success=True, output_valid=True,
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.json.return_value = {"trace_id": "xyz"}
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await submit_trace({"trace": trace.model_dump()})
    assert result["submitted"] is True


def test_register_alembic_hook_adds_post_stage_hook():
    registry = HookRegistry()
    register_alembic_hook(registry, threshold=0.8)
    assert len(registry._hooks[HookPhase.POST_STAGE]) == 1
