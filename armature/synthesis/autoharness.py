"""AutoHarness — iterative harness spec synthesis from natural language.

SpecDrafter calls a frontier LLM to produce a YAML spec from a task description.
AutoHarness orchestrates the draft → validate → constraint-check → refine cycle,
accumulating feedback from each failed attempt so the LLM can self-correct.
"""
from __future__ import annotations
import json
from typing import Callable
import litellm
from armature.spec.models import HarnessSpec
from armature.spec.validator import validate_spec, SpecValidationError


async def litellm_completion(**kwargs):
    return await litellm.acompletion(**kwargs)


_SYSTEM_PROMPT = """\
You are an expert at writing Armature harness specs in YAML.

Armature harness specs define multi-stage LLM workflows. Generate a valid YAML spec \
for the task described by the user. The spec must include:
- name: a snake_case workflow name
- stages: a list of stage objects, each with an id and a role

A minimal valid stage looks like:
  id: worker
  role:
    name: worker
    type: worker        # one of: worker, orchestrator, judge, researcher
    description: >
      Describe what this stage does.

Return ONLY the raw YAML — no markdown fences, no explanation.
"""

_REFINE_INSTRUCTION = """\

Previous attempt failed with the following feedback. Fix these issues and try again:
{feedback}
"""


class SpecDrafter:
    """Calls a frontier LLM to draft HarnessSpec YAML from a task description."""

    def __init__(self, model: str):
        self._model = model

    async def draft(self, task_description: str, feedback: str | None = None) -> str:
        """Return raw YAML string from the LLM (may be invalid)."""
        user_content = task_description
        if feedback:
            user_content += _REFINE_INSTRUCTION.format(feedback=feedback)

        response = await litellm_completion(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        return response.choices[0].message.content or ""

    def parse(self, yaml_text: str) -> HarnessSpec | None:
        """Parse raw LLM output into a HarnessSpec; returns None on any failure."""
        import yaml as _yaml

        text = yaml_text.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                inner = text[first_newline + 1:]
                fence_end = inner.rfind("```")
                if fence_end != -1:
                    inner = inner[:fence_end]
                text = inner.strip()

        try:
            data = _yaml.safe_load(text)
        except _yaml.YAMLError:
            return None

        if not isinstance(data, dict):
            return None
        if "stages" not in data:
            return None

        try:
            return HarnessSpec(**data, validate=False)
        except Exception:
            return None


class AutoHarness:
    """Synthesizes a HarnessSpec through iterative draft → validate → refine cycles."""

    def __init__(self, drafter: SpecDrafter, max_iterations: int = 5):
        self._drafter = drafter
        self._max_iterations = max_iterations

    async def synthesize(
        self,
        task_description: str,
        constraint_fn: Callable[[HarnessSpec], bool] | None = None,
    ) -> tuple[HarnessSpec | None, list[str]]:
        """Draft and refine a spec until it validates and satisfies the constraint.

        Returns (spec, feedback_history).
        spec is None when all iterations are exhausted.
        feedback_history contains one entry per failed iteration.
        """
        feedback: str | None = None
        history: list[str] = []

        for _ in range(self._max_iterations):
            yaml_text = await self._drafter.draft(task_description, feedback=feedback)

            spec = self._drafter.parse(yaml_text)
            if spec is None:
                feedback = "The generated YAML could not be parsed. Produce valid YAML only."
                history.append(feedback)
                continue

            # Validate spec structure
            try:
                validate_spec(spec, strict=True)
            except SpecValidationError as exc:
                error_lines = "\n".join(
                    f"  [{e.code}] {e.message}" for e in exc.errors
                )
                feedback = f"Spec validation failed:\n{error_lines}"
                history.append(feedback)
                continue

            # Check domain constraint if provided
            if constraint_fn is not None and not constraint_fn(spec):
                feedback = "The spec did not satisfy the domain constraint. Revise the spec."
                history.append(feedback)
                continue

            return spec, history

        return None, history
