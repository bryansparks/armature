"""Stub mode: every stage returns a recorded output instead of running, so a workflow can be tested for free.

``ARMATURE_STUB_OUTPUTS`` names a JSON file mapping stage id to output. A list
stands for a fan-out stage's branches, one item per branch. A stage with no
entry fails with STUB_MISSING, unless ``ARMATURE_STUB_DEFAULT=empty``, and an
LLM stage with no entry returns a placeholder without calling a model. Gates
approve themselves.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from armature.nodes.base import BaseNode
from armature.spec.models import Stage

OUTPUTS_ENV = "ARMATURE_STUB_OUTPUTS"
DEFAULT_ENV = "ARMATURE_STUB_DEFAULT"
PLACEHOLDER = {"stub": True}


class StubMissing(ValueError):
    def __init__(self, stage_id: str, detail: str = "no recorded output"):
        super().__init__(f"STUB_MISSING: stage '{stage_id}' has {detail} in stub mode")


class StubOutputs:
    def __init__(self, outputs: dict[str, Any], *, missing_empty: bool = False):
        self.outputs = outputs
        self.missing_empty = missing_empty

    @classmethod
    def from_env(cls) -> StubOutputs | None:
        path = os.environ.get(OUTPUTS_ENV, "").strip()
        if not path:
            return None
        outputs = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(outputs, dict):
            raise ValueError(f"{OUTPUTS_ENV} must name a JSON object of stage id to output")
        return cls(outputs, missing_empty=os.environ.get(DEFAULT_ENV, "").strip() == "empty")

    def output_for(self, stage: Stage, branch: int | None) -> Any:
        if stage.id not in self.outputs:
            if stage.gate == "human":
                return {"approved": True, "feedback": None, **PLACEHOLDER}
            if stage.role is not None and not (stage.adapter or stage.tool_call or stage.subagent_spec):
                return dict(PLACEHOLDER)
            if self.missing_empty:
                return {}
            raise StubMissing(stage.id)
        value = self.outputs[stage.id]
        if branch is not None and isinstance(value, list):
            if branch < len(value):
                return copy.deepcopy(value[branch])
            if self.missing_empty:
                return {}
            raise StubMissing(stage.id, f"no recorded output for branch {branch}")
        return copy.deepcopy(value)


class StubNode(BaseNode):
    def __init__(self, stage: Stage, outputs: StubOutputs, branch: int | None = None):
        self._stage = stage
        self._outputs = outputs
        self._branch = branch

    async def execute(self, context: dict[str, Any]) -> Any:
        return self._outputs.output_for(self._stage, self._branch)
