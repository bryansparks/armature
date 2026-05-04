from __future__ import annotations
from pathlib import Path
from typing import Any
from armature.nodes.base import BaseNode
from armature.spec.models import Stage
from armature.spec.loader import load_spec


class SubagentNode(BaseNode):
    def __init__(self, stage: Stage, session_dir: Path | None = None):
        if not stage.subagent_spec:
            raise ValueError(f"Stage '{stage.id}' has no subagent_spec")
        self._stage = stage
        self._session_dir = session_dir

    async def execute(self, context: dict[str, Any]) -> Any:
        spec_path = Path(self._stage.subagent_spec)
        if not spec_path.exists():
            raise FileNotFoundError(f"Subagent spec not found: {spec_path}")

        # Import here to avoid circular import (engine imports subagent)
        from armature.runtime.engine import Harness

        child = Harness(
            spec=load_spec(spec_path, vars=context),
            session_dir=self._session_dir,
        )
        return await child.run(context)
