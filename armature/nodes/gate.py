from __future__ import annotations
from typing import Any
from jinja2 import Environment, BaseLoader
from armature.nodes.base import BaseNode
from armature.spec.models import Stage


class HumanGateNode(BaseNode):
    def __init__(self, stage: Stage):
        self._stage = stage

    async def execute(self, context: dict[str, Any]) -> Any:
        message = self._stage.present or "Review required."
        if "{{" in message:
            env = Environment(loader=BaseLoader(), variable_start_string="{{", variable_end_string="}}")
            message = env.from_string(message).render(**context)

        print(f"\n{'='*60}")
        print(f"HUMAN APPROVAL REQUIRED")
        print(f"{'='*60}")
        print(message)
        print(f"{'='*60}")

        response = input("Approve? [yes/no/feedback]: ").strip().lower()
        if response in ("yes", "y", "approve"):
            return {"approved": True, "feedback": None}
        else:
            feedback = input("Enter feedback (press Enter to skip): ").strip()
            return {"approved": False, "feedback": feedback or response}
