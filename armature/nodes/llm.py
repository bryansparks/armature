from __future__ import annotations
import asyncio
import json
import os
import random
from typing import Any
import litellm
from armature.nodes.base import BaseNode
from armature.spec.models import Stage, ModelTiers, RoleType, RoleTypeDefaults
from armature.runtime.prompt import PromptAssembler


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


def _extract_json_from_response(content: str) -> dict | None:
    """Find the last complete JSON object in content.

    Handles two common patterns from reasoning/non-OpenAI models:
    1. JSON wrapped in ```json ... ``` markdown code fences.
    2. JSON preceded by <think> blocks or tool-call markup — scan backwards
       from the end to find the payload without being tripped up by smaller
       JSON fragments in the reasoning text.
    """
    stripped = content.strip()

    # Try stripping a markdown code fence first
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            inner = stripped[first_newline + 1:]
            fence_end = inner.rfind("```")
            if fence_end != -1:
                inner = inner[:fence_end]
            try:
                return json.loads(inner.strip())
            except json.JSONDecodeError:
                pass

    # Scan backwards for the last complete JSON object
    pos = len(content)
    while pos > 0:
        pos = content.rfind("{", 0, pos)
        if pos < 0:
            return None
        try:
            return json.loads(content[pos:])
        except json.JSONDecodeError:
            pass
    return None


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
    """Call litellm with retry on transient errors.

    Args:
        max_retries: Total number of attempts (not retries after the first).
                     Default 3 = 1 initial attempt + 2 retries.
                     Note: LoopConfig.max in stage recovery means retries after
                     the first attempt, so LoopConfig.max=2 also yields 3 total calls.
    """
    retryable = _RETRYABLE_ERRORS
    last_exc: Exception = RuntimeError(f"_call_with_retry called with max_retries={max_retries}")

    for attempt in range(max_retries):
        try:
            from armature.telemetry import get_tracer
            with get_tracer().start_as_current_span(
                "armature.llm.completion",
                attributes={"model": model, "attempt": attempt},
            ) as span:
                response = await litellm_completion(model=model, **kwargs)
                try:
                    usage = getattr(response, "usage", None)
                    span.set_attribute("input_tokens", getattr(usage, "prompt_tokens", 0) or 0)
                    span.set_attribute("output_tokens", getattr(usage, "completion_tokens", 0) or 0)
                except Exception:
                    pass  # telemetry must never break execution
            return response
        except retryable as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0.0, 0.5)
                await asyncio.sleep(delay)

    raise last_exc


# Canonical tier ordering for escalation. Only configured tiers participate.
_CANONICAL_TIER_ORDER = ["tiny", "small", "medium", "large", "frontier"]

# Providers that do not support response_format / structured output.
_NO_STRUCTURED_OUTPUT = {"ollama"}


class LLMNode(BaseNode):
    def __init__(
        self,
        stage: Stage,
        tiers: ModelTiers,
        role_type_defaults: RoleTypeDefaults | None = None,
        assembler: PromptAssembler | None = None,
        registry=None,
        transcript: list[dict] | None = None,
        workflow_name: str = "",
    ):
        if stage.role is None:
            raise ValueError(f"Stage '{stage.id}' has no role — cannot create LLMNode")
        self._stage = stage
        self._tiers = tiers
        self._role_type_defaults = role_type_defaults or RoleTypeDefaults()
        self._assembler = assembler or PromptAssembler()
        self._registry = registry
        self._transcript = transcript
        self._workflow_name = workflow_name
        self._bootstrap_store = None
        self._max_tool_iterations = 10

    def _active_tier_order(self) -> list[str]:
        """Tiers actually configured in the spec, in canonical escalation order."""
        return [t for t in _CANONICAL_TIER_ORDER if getattr(self._tiers, t, None) is not None]

    def _resolve_tier_name(self) -> str:
        """Return the tier name for this stage's role.

        Priority: explicit role.model_tier → role_type_defaults → first configured tier.
        """
        role = self._stage.role
        if role.model_tier is not None:
            return role.model_tier
        defaults = self._role_type_defaults
        return getattr(defaults, role.type.value, None) or self._active_tier_order()[0]

    def _model_string(self, tier_config) -> str:
        provider = tier_config.provider
        model = tier_config.model
        if provider == "ollama":
            return f"ollama/{model}"
        elif provider == "openrouter":
            return f"openrouter/{model}"
        return model

    def _tier_extra_kwargs(self, tier_config) -> dict[str, Any]:
        """Build litellm kwargs that vary per tier (api_base, auth, temperature, max_tokens).

        Resolution order for temperature and max_tokens:
          role-level override → tier-level default → omitted (model default)
        """
        extra: dict[str, Any] = {}

        if tier_config.api_base:
            extra["api_base"] = tier_config.api_base

        if tier_config.api_key_env:
            key = os.environ.get(tier_config.api_key_env)
            if key:
                extra["api_key"] = key
        elif tier_config.provider == "ollama":
            key = os.environ.get("OLLAMA_API_KEY")
            if key:
                extra["api_key"] = key

        role = self._stage.role
        temp = role.temperature if role.temperature is not None else tier_config.temperature
        if temp is not None:
            extra["temperature"] = temp

        max_tok = role.max_tokens if role.max_tokens is not None else tier_config.max_tokens
        if max_tok is not None:
            extra["max_tokens"] = max_tok

        return extra

    def _supports_tool_calling(self, tier_config) -> bool:
        """Whether this tier should receive native tool specs.

        Explicit tier_config.tool_calling takes priority over provider heuristic.
        """
        if tier_config.tool_calling is not None:
            return tier_config.tool_calling
        return tier_config.provider not in _NO_STRUCTURED_OUTPUT

    def _response_format_kwargs(self, tier_config) -> dict[str, Any]:
        """Build the response_format kwarg appropriate for this specific tier's provider.

        Non-OpenAI-compatible providers (Ollama, etc.) don't support response_format —
        those fall back to prompt-guided JSON + extraction. Re-evaluated per escalation
        tier so that switching providers mid-escalation picks the right strategy.
        """
        if self._stage.output_mode.value not in ("json", "guided_json"):
            return {}
        if tier_config.provider in _NO_STRUCTURED_OUTPUT:
            return {}  # rely on prompt + _extract_json_from_response
        if self._stage.output_schema:
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"{self._stage.id}_output",
                        "strict": True,
                        "schema": self._stage.output_schema,
                    },
                }
            }
        return {"response_format": {"type": "json_object"}}

    def _resolve_model(self) -> str:
        tier_name = self._resolve_tier_name()
        tier_config = getattr(self._tiers, tier_name, None)
        if tier_config is None:
            active = self._active_tier_order()
            if not active:
                raise ValueError("No model tiers configured in spec")
            tier_config = getattr(self._tiers, active[0])
        return self._model_string(tier_config)

    def _append_transcript(self, messages: list[dict], model: str, response: str) -> None:
        if self._transcript is None:
            return
        role = self._stage.role
        self._transcript.append({
            "stage_id": self._stage.id,
            "role_name": role.name,
            "role_type": role.type.value,
            "model": model,
            "system_prompt": messages[0]["content"] if messages else "",
            "response": response,
        })

    async def execute(self, context: dict[str, Any]) -> Any:
        role = self._stage.role
        # Per-stage filtering: only tools explicitly declared on the role are exposed.
        # Empty role.tools → no tools in prompt, no dispatch (clean by default).
        if self._registry and role.tools:
            all_descriptors = {t["name"]: t for t in self._registry.descriptors()}
            stage_tools = [all_descriptors[name] for name in role.tools if name in all_descriptors]
        else:
            stage_tools = []

        output_schema = self._stage.output_schema if self._stage.output_mode.value == "guided_json" else None

        examples: list[dict] = []
        if self._bootstrap_store is not None:
            examples = await self._bootstrap_store.examples_for_stage(
                workflow_name=self._workflow_name,
                stage_id=self._stage.id,
            )

        system_prompt = self._assembler.build(
            role=role,
            tools=stage_tools,
            context=context,
            signature=self._stage.signature,
            output_schema=output_schema,
            examples=examples,
        )

        # Apply the same signature.input filter to the user message that PromptAssembler
        # applies to ## Current Context — prevents large upstream outputs from leaking in.
        sig = self._stage.signature
        if sig and sig.input:
            visible_context = {k: v for k, v in context.items() if k in sig.input}
        else:
            visible_context = context

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(visible_context, default=str)},
        ]

        is_json_mode = self._stage.output_mode.value in ("json", "guided_json")
        return await self._execute_with_escalation(messages, is_json_mode, stage_tools)

    async def _execute_with_escalation(
        self, messages: list[dict], parse_as_json: bool, stage_tools: list[dict] | None = None
    ) -> Any:
        tier_name = self._resolve_tier_name()
        active_order = self._active_tier_order()
        tried: set[str] = set()
        content = ""
        tier_attempt = -1

        for attempt_tier in [tier_name] + active_order:
            if attempt_tier in tried:
                continue
            tier_config = getattr(self._tiers, attempt_tier, None)
            if tier_config is None:
                continue
            tried.add(attempt_tier)
            tier_attempt += 1

            model = self._model_string(tier_config)
            kwargs = {
                "messages": messages,
                **self._tier_extra_kwargs(tier_config),
                **self._response_format_kwargs(tier_config),
            }

            # Inject native tool specs when the tier supports tool calling.
            # Explicit tier_config.tool_calling overrides the provider heuristic.
            if stage_tools and self._supports_tool_calling(tier_config):
                kwargs["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": {
                                "type": "object",
                                "properties": t["parameters"],
                                "required": [
                                    k for k, v in t["parameters"].items()
                                    if not v.get("optional")
                                ],
                            },
                        },
                    }
                    for t in stage_tools
                ]
                kwargs["tool_choice"] = "auto"

            response = await _call_with_retry(model=model, **kwargs)
            msg = response.choices[0].message
            content = msg.content or ""

            # ReAct tool-call loop — iterations reset per tier attempt.
            # messages is mutated in place; kwargs["messages"] is the same object.
            iterations = 0
            while getattr(msg, "tool_calls", None) and iterations < self._max_tool_iterations:
                iterations += 1
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                        tool_result = await self._registry.dispatch(tc.function.name, args)
                    except Exception as exc:
                        tool_result = {"error": str(exc)}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_result, default=str),
                    })
                response = await _call_with_retry(model=model, **kwargs)
                msg = response.choices[0].message
                content = msg.content or ""

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0

            if parse_as_json:
                result = None
                try:
                    result = json.loads(content)
                except json.JSONDecodeError:
                    result = _extract_json_from_response(content)
                if result is not None and not isinstance(result, dict):
                    result = _extract_json_from_response(content)
                if result is not None:
                    self._append_transcript(messages, model, content)
                    result["_input_tokens"] = input_tokens
                    result["_output_tokens"] = output_tokens
                    result["_escalation_count"] = tier_attempt
                    return result
                continue  # escalate to next tier

            if content:
                self._append_transcript(messages, model, content)
                return {"content": content, "_input_tokens": input_tokens, "_output_tokens": output_tokens, "_escalation_count": tier_attempt}
            continue  # empty text response — escalate to next tier

        # All tiers exhausted
        if not parse_as_json:
            # All tiers returned empty content — report as empty, not a parse error
            return {"content": "", "_input_tokens": 0, "_output_tokens": 0, "_escalation_count": tier_attempt}
        return {"raw": content, "_parse_error": True, "_input_tokens": 0, "_output_tokens": 0, "_escalation_count": tier_attempt}
