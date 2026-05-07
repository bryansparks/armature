import pytest
import litellm
from unittest.mock import AsyncMock, MagicMock, patch
from armature.nodes.llm import LLMNode
from armature.spec.models import Stage, Role, RoleType, ModelTiers, ModelTierConfig, OutputMode

def make_stage(role_type: RoleType = RoleType.WORKER) -> Stage:
    return Stage(
        id="test",
        role=Role(name="r", type=role_type, description="test role", model_tier="small"),
    )

def make_tiers() -> ModelTiers:
    return ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-opus-4-7"),
    )

async def test_worker_routes_to_small_model():
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)
    model_str = node._resolve_model()
    assert "qwen" in model_str or "ollama" in model_str.lower()

async def test_judge_routes_to_frontier_model():
    stage = make_stage(RoleType.JUDGE)
    stage.role.model_tier = "frontier"
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)
    model_str = node._resolve_model()
    assert "claude" in model_str or "anthropic" in model_str.lower()

def test_llm_node_requires_role():
    stage = Stage(id="no-role", role=None)
    with pytest.raises(ValueError, match="role"):
        LLMNode(stage=stage, tiers=ModelTiers())


def make_litellm_response(content: str, input_tokens: int = 10, output_tokens: int = 5):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.choices[0].message.tool_calls = None  # MagicMock attributes are truthy; be explicit
    response.usage = MagicMock()
    response.usage.prompt_tokens = input_tokens
    response.usage.completion_tokens = output_tokens
    return response


async def test_guided_json_passes_response_format_for_openai_provider():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    stage.output_schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    node = LLMNode(stage=stage, tiers=tiers)

    captured_kwargs = {}

    async def mock_completion(**kwargs):
        captured_kwargs.update(kwargs)
        return make_litellm_response('{"score": 0.9}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert "response_format" in captured_kwargs
    assert result["score"] == pytest.approx(0.9)


async def test_guided_json_omits_response_format_for_ollama():
    # Ollama does not support response_format — we rely on prompt + extraction instead.
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    stage.output_schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    tiers = make_tiers()  # small = ollama
    node = LLMNode(stage=stage, tiers=tiers)

    captured_kwargs = {}

    async def mock_completion(**kwargs):
        captured_kwargs.update(kwargs)
        return make_litellm_response('{"score": 0.9}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert "response_format" not in captured_kwargs
    assert result["score"] == pytest.approx(0.9)


async def test_tier_escalation_on_parse_failure():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    tiers = ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        medium=ModelTierConfig(provider="ollama", model="qwen2.5:14b"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response("not valid json")
        return make_litellm_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2  # first call failed, escalated to medium
    assert result.get("ok") is True
    assert "_parse_error" not in result


async def test_no_escalation_if_no_higher_tier():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    # Only small tier configured — no escalation target
    tiers = ModelTiers(small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"))
    node = LLMNode(stage=stage, tiers=tiers)

    async def mock_completion(**kwargs):
        return make_litellm_response("not valid json")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert result.get("_parse_error") is True  # gracefully returns parse error


# ---------------------------------------------------------------------------
# Task 2: LLM retry with exponential backoff
# ---------------------------------------------------------------------------

async def test_retries_on_rate_limit_and_succeeds():
    """Should retry on RateLimitError and eventually return a valid result."""
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise litellm.RateLimitError(
                message="rate limited",
                llm_provider="openai",
                model="test-model",
            )
        return make_litellm_response("hello")

    mock_sleep = AsyncMock()
    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion), \
         patch("asyncio.sleep", mock_sleep):
        result = await node.execute({})

    assert call_count == 3
    assert result.get("content") == "hello"
    assert mock_sleep.call_count == 2  # 2 failures → 2 sleeps before 3rd attempt


async def test_raises_after_max_retries():
    """Should re-raise RateLimitError after exhausting all retries."""
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    async def always_rate_limit(**kwargs):
        raise litellm.RateLimitError(
            message="always limited",
            llm_provider="openai",
            model="test-model",
        )

    with patch("armature.nodes.llm.litellm_completion", side_effect=always_rate_limit), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(litellm.RateLimitError):
            await node.execute({})


async def test_non_transient_error_not_retried():
    """AuthenticationError should propagate immediately without any retry."""
    stage = make_stage(RoleType.WORKER)
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def auth_error(**kwargs):
        nonlocal call_count
        call_count += 1
        raise litellm.AuthenticationError(
            message="bad key",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    with patch("armature.nodes.llm.litellm_completion", side_effect=auth_error), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(litellm.AuthenticationError):
            await node.execute({})

    assert call_count == 1  # fired once, not retried


async def test_escalation_count_zero_on_first_tier_success():
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    async def mock_completion(**kwargs):
        return make_litellm_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    # No escalation needed — count should be 0 (popped by engine, but returned in raw result here)
    # Since execute() returns after engine pops it, we check the LLM node directly
    stage2 = make_stage(RoleType.WORKER)
    stage2.output_mode = OutputMode.GUIDED_JSON
    tiers2 = make_tiers()
    node2 = LLMNode(stage=stage2, tiers=tiers2)

    async def mock_completion2(**kwargs):
        return make_litellm_response('{"score": 0.9}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion2):
        result2 = await node2._execute_with_escalation(
            [{"role": "system", "content": "test"}, {"role": "user", "content": "{}"}],
            parse_as_json=True,
        )
    assert result2.get("_escalation_count") == 0


async def test_escalation_count_one_on_first_escalation():
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    tiers = ModelTiers(
        small=ModelTierConfig(provider="ollama", model="qwen2.5:7b"),
        medium=ModelTierConfig(provider="ollama", model="qwen2.5:14b"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response("not valid json")
        return make_litellm_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node._execute_with_escalation(
            [{"role": "system", "content": "test"}, {"role": "user", "content": "{}"}],
            parse_as_json=True,
        )

    assert call_count == 2
    assert result.get("_escalation_count") == 1  # escalated once


# ── Fix #2: signature.input filters user message ────────────────────────────

async def test_signature_input_filters_user_message():
    """signature.input should filter the JSON user message, not just the system prompt."""
    import json as _json
    from armature.spec.models import Signature

    stage = make_stage(RoleType.WORKER)
    stage.signature = Signature(input={"topic": "The topic"})
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    node = LLMNode(stage=stage, tiers=tiers)

    captured_user_msg: str = ""

    async def mock_completion(**kwargs):
        nonlocal captured_user_msg
        for m in kwargs["messages"]:
            if m["role"] == "user":
                captured_user_msg = m["content"]
        return make_litellm_response("done")

    context = {"topic": "AI safety", "large_payload": "x" * 10_000, "internal_id": "abc"}
    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute(context)

    user_data = _json.loads(captured_user_msg)
    assert "topic" in user_data
    assert "large_payload" not in user_data
    assert "internal_id" not in user_data


async def test_no_signature_user_message_passes_full_context():
    """Without signature.input, user message still gets the full context."""
    import json as _json

    stage = make_stage(RoleType.WORKER)
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    node = LLMNode(stage=stage, tiers=tiers)

    captured_user_msg: str = ""

    async def mock_completion(**kwargs):
        nonlocal captured_user_msg
        for m in kwargs["messages"]:
            if m["role"] == "user":
                captured_user_msg = m["content"]
        return make_litellm_response("done")

    context = {"topic": "AI", "internal_id": "abc"}
    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute(context)

    user_data = _json.loads(captured_user_msg)
    assert "topic" in user_data
    assert "internal_id" in user_data


# ── Bug 1: JSON array instead of object does not crash ───────────────────────

async def test_json_array_response_escalates_not_crashes():
    """Model returning a bare JSON array [] should escalate, not raise TypeError."""
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    tiers = ModelTiers(
        small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
        frontier=ModelTierConfig(provider="openai", model="gpt-4o"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response('[{"item": "x"}, {"item": "y"}]')
        return make_litellm_response('{"ok": true}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2
    assert result.get("ok") is True
    assert "_parse_error" not in result


async def test_json_primitive_array_escalates():
    """Bare numeric array [1, 2, 3] also escalates cleanly."""
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    tiers = ModelTiers(
        small=ModelTierConfig(provider="openai", model="gpt-4o-mini"),
        frontier=ModelTierConfig(provider="openai", model="gpt-4o"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response('[1, 2, 3]')
        return make_litellm_response('{"count": 3}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2
    assert result.get("count") == 3


# ── Bug 2: output_schema in system prompt ────────────────────────────────────

async def test_guided_json_schema_appears_in_system_prompt():
    """output_schema is injected into the system prompt for all providers."""
    stage = make_stage(RoleType.WORKER)
    stage.output_mode = OutputMode.GUIDED_JSON
    stage.output_schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    tiers = ModelTiers(small=ModelTierConfig(provider="ollama", model="llama3"))
    node = LLMNode(stage=stage, tiers=tiers)

    captured_system = ""

    async def mock_completion(**kwargs):
        nonlocal captured_system
        for m in kwargs["messages"]:
            if m["role"] == "system":
                captured_system = m["content"]
        return make_litellm_response('{"score": 0.8}')

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert "Required Output Format" in captured_system
    assert '"score"' in captured_system
    assert result.get("score") == pytest.approx(0.8)


async def test_text_mode_no_schema_in_prompt():
    """Non-guided_json stages must not inject a schema section."""
    stage = make_stage(RoleType.WORKER)
    # output_mode defaults to text
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    captured_system = ""

    async def mock_completion(**kwargs):
        nonlocal captured_system
        for m in kwargs["messages"]:
            if m["role"] == "system":
                captured_system = m["content"]
        return make_litellm_response("plain text response")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        await node.execute({})

    assert "Required Output Format" not in captured_system


# ── Bug 1: empty text response escalates instead of returning blank ───────────

async def test_empty_text_response_escalates_to_next_tier():
    """Empty content in text mode should escalate, not return {"content": ""}."""
    stage = make_stage(RoleType.WORKER)
    # output_mode defaults to text
    tiers = ModelTiers(
        small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_litellm_response("")   # empty first response
        return make_litellm_response("actual response content")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2
    assert result["content"] == "actual response content"


async def test_none_content_escalates():
    """msg.content = None (provider quirk) is treated the same as empty."""
    stage = make_stage(RoleType.WORKER)
    tiers = ModelTiers(
        small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            resp = make_litellm_response("")
            resp.choices[0].message.content = None   # simulate None from provider
            return resp
        return make_litellm_response("recovered content")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 2
    assert result["content"] == "recovered content"


async def test_non_empty_text_response_returned_immediately():
    """Non-empty text response still returns on the first tier without escalation."""
    stage = make_stage(RoleType.WORKER)
    tiers = ModelTiers(
        small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        return make_litellm_response("good response")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion):
        result = await node.execute({})

    assert call_count == 1
    assert result["content"] == "good response"


async def test_all_tiers_empty_returns_empty_not_parse_error():
    """If every tier returns empty content, result is {"content": ""} — not _parse_error."""
    stage = make_stage(RoleType.WORKER)
    tiers = ModelTiers(
        small=ModelTierConfig(provider="anthropic", model="claude-haiku-4-5-20251001"),
        frontier=ModelTierConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    node = LLMNode(stage=stage, tiers=tiers)

    async def always_empty(**kwargs):
        return make_litellm_response("")

    with patch("armature.nodes.llm.litellm_completion", side_effect=always_empty):
        result = await node.execute({})

    assert result.get("content") == ""
    assert "_parse_error" not in result
