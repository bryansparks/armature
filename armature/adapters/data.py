"""Shared training-data format for adapter factories.

Backends that produce LoRA adapters consume and produce the same internal
representation so that S2L, trace-based, and merged adapters can be evaluated
and stored uniformly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TrainingExample:
    """One supervised fine-tuning example in OpenAI chat-message format."""

    messages: list[dict[str, Any]]
    skill_id: str | None = None
    source: str = ""  # "skill" | "trace" | "merge"
    score: float | None = None  # optional quality score from a judge/quorum


@dataclass
class TrainingDataset:
    """A collection of training examples plus metadata about their origin."""

    examples: list[TrainingExample] = field(default_factory=list)
    base_model: str | None = None
    name: str | None = None

    def save_jsonl(self, path: Path) -> None:
        """Serialize examples as JSONL."""
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for ex in self.examples:
                f.write(json.dumps(self._example_to_dict(ex), default=str) + "\n")

    def _example_to_dict(self, ex: TrainingExample) -> dict[str, Any]:
        return {
            "messages": ex.messages,
            "skill_id": ex.skill_id,
            "source": ex.source,
            "score": ex.score,
        }
