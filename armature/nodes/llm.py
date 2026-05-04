from __future__ import annotations
import asyncio
import json
import random
from typing import Any
import litellm
from armature.nodes.base import BaseNode
from armature.spec.models import Stage, ModelTiers, RoleType
from armature.runtime.prompt import PromptAssembler


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


def _retryable_errors() -> tuple[type[Exception], ...]:
    errors: list[type[Exception]] = []
    for name in ("RateLimitError", "ServiceUnavailableError", "APIConnectionError", "Timeout"):
        cls = getattr(litellm, name, None)
        if cls is not None:
            errors.append(cls)
    return tuple(errors) if errors else (Exception,)


_RETRYABLE_ERRORS = _retryable_errors()


async def _call_with_retry(
    model: str,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any:
    retryable = _RETRYABLE_ERRORS
    last_exc: Exception = RuntimeError(f"_call_with_retry called with max_retries={max_retries}")

    for attempt in range(max_retries):
        try:
            response = await litellm_completion(model=model, **kwargs)
            try:
                from armature.telemetry import get_tracer
                usage = getattr(response, "usage", None)
                with get_tracer().start_as_current_span(
                    "armature.llm.completion",
                    attributes={
                        "model": model,
                        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                        "attempt": attempt,
                    },
                ):
                    pass
            except Exception:
                pass  # telemetry must never break execution
            return response
        except retryable as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0.0, 0.5)
                await asyncio.sleep(delay)

    raise last_exc


_TIER_ORDER = ["tiny", "small", "medium", "large", "frontier"]


class LLMNode(BaseNode):
    def __init__(
        self,
        stage: Stage,
        tiers: ModelTiers,
        assembler: PromptAssembler | None = None,
        registry=None,
    ):
        if stage.role is None:
            raise ValueError(f"Stage '{stage.id}' has no role — cannot create LLMNode")
        self._stage = stage
        self._tiers = tiers
        self._assembler = assembler or PromptAssembler()
        self._registry = registry

    def _model_string(self, tier_config) -> str:
        provider = tier_config.provider
        model = tier_config.model
        if provider == "ollama":
            return f"ollama/{model}"
        elif provider == "openrouter":
            return f"openrouter/{model}"
        return model

    def _resolve_model(self) -> str:
        tier_name = self._stage.role.model_tier
        tier_config = getattr(self._tiers, tier_name, None)
        if tier_config is None:
            for t in _TIER_ORDER:
                cfg = getattr(self._tiers, t, None)
                if cfg is not None:
                    tier_config = cfg
                    break
        if tier_config is None:
            raise ValueError(f"No model tier configured for '{tier_name}'")
        return self._model_string(tier_config)

    async def execute(self, context: dict[str, Any]) -> Any:
        role = self._stage.role
        tools = self._registry.descriptors() if self._registry else []
        system_prompt = self._assembler.build(role=role, tools=tools, context=context)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, default=str)},
        ]

        kwargs: dict[str, Any] = {"messages": messages}

        is_json_mode = self._stage.output_mode.value in ("json", "guided_json")
        if is_json_mode and self._stage.output_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{self._stage.id}_output",
                    "strict": True,
                    "schema": self._stage.output_schema,
                },
            }
        elif is_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        return await self._execute_with_escalation(kwargs, is_json_mode)

    async def _execute_with_escalation(
        self, base_kwargs: dict[str, Any], parse_as_json: bool
    ) -> Any:
        tier_name = self._stage.role.model_tier
        tried: set[str] = set()
        content = ""

        for attempt_tier in [tier_name] + _TIER_ORDER:
            if attempt_tier in tried:
                continue
            tier_config = getattr(self._tiers, attempt_tier, None)
            if tier_config is None:
                continue
            tried.add(attempt_tier)

            model = self._model_string(tier_config)
            response = await _call_with_retry(model=model, **base_kwargs)
            content = response.choices[0].message.content

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0

            if parse_as_json:
                try:
                    result = json.loads(content)
                    result["_input_tokens"] = input_tokens
                    result["_output_tokens"] = output_tokens
                    return result
                except json.JSONDecodeError:
                    continue  # escalate to next tier

            return {"content": content, "_input_tokens": input_tokens, "_output_tokens": output_tokens}

        # All tiers exhausted without a valid parse
        return {"raw": content, "_parse_error": True, "_input_tokens": 0, "_output_tokens": 0}
