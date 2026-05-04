from __future__ import annotations
import json
from typing import Any
import litellm
from armature.nodes.base import BaseNode
from armature.spec.models import Stage, ModelTiers, RoleType
from armature.runtime.prompt import PromptAssembler


async def litellm_completion(**kwargs) -> Any:
    return await litellm.acompletion(**kwargs)


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

        provider = tier_config.provider
        model = tier_config.model
        if provider == "ollama":
            return f"ollama/{model}"
        elif provider == "anthropic":
            return model
        elif provider == "openrouter":
            return f"openrouter/{model}"
        return model

    async def execute(self, context: dict[str, Any]) -> Any:
        role = self._stage.role
        tools = self._registry.descriptors() if self._registry else []
        system_prompt = self._assembler.build(role=role, tools=tools, context=context)
        model = self._resolve_model()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, default=str)},
        ]

        response = await litellm_completion(model=model, messages=messages)
        content = response.choices[0].message.content

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0

        if self._stage.output_mode.value in ("json", "guided_json"):
            try:
                parsed = json.loads(content)
                parsed["_input_tokens"] = input_tokens
                parsed["_output_tokens"] = output_tokens
                return parsed
            except json.JSONDecodeError:
                return {"raw": content, "_parse_error": True, "_input_tokens": input_tokens, "_output_tokens": output_tokens}
        return {"content": content, "_input_tokens": input_tokens, "_output_tokens": output_tokens}
