"""Trace preprocessing utilities for adapter training.

Exported SFT/DPO traces often contain noise: duplicates, low-quality examples,
or oversized conversations. Preparing them before fine-tuning improves both the
adapter and the merge that may follow.
"""
from __future__ import annotations

from dataclasses import dataclass

from armature.adapters.data import TrainingExample


@dataclass
class PreprocessConfig:
    """Controls how raw trace examples are cleaned before training."""

    min_score: float | None = None
    max_total_length: int | None = None
    max_examples: int | None = None
    deduplicate: bool = True


def preprocess_examples(
    examples: list[TrainingExample],
    config: PreprocessConfig,
) -> list[TrainingExample]:
    """Apply score, length, dedup, and limit filters in order."""
    examples = _drop_empty(examples)
    if config.min_score is not None:
        examples = _filter_by_score(examples, config.min_score)
    if config.max_total_length is not None:
        examples = _filter_by_length(examples, config.max_total_length)
    if config.deduplicate:
        examples = _deduplicate(examples)
    if config.max_examples is not None:
        examples = _limit(examples, config.max_examples)
    return examples


def _drop_empty(examples: list[TrainingExample]) -> list[TrainingExample]:
    """Remove examples with no assistant content."""
    return [
        ex
        for ex in examples
        if ex.messages and any(m.get("role") == "assistant" for m in ex.messages)
    ]


def _filter_by_score(
    examples: list[TrainingExample],
    min_score: float,
) -> list[TrainingExample]:
    """Keep examples whose quality score is at least ``min_score``."""
    return [
        ex
        for ex in examples
        if ex.score is not None and ex.score >= min_score
    ]


def _filter_by_length(
    examples: list[TrainingExample],
    max_total_length: int,
) -> list[TrainingExample]:
    """Keep examples whose total message length is under ``max_total_length``."""
    return [
        ex
        for ex in examples
        if sum(len(str(m.get("content", ""))) for m in ex.messages) <= max_total_length
    ]


def _deduplicate(examples: list[TrainingExample]) -> list[TrainingExample]:
    """Remove exact-duplicate message sequences, preserving order."""
    seen: set[str] = set()
    result: list[TrainingExample] = []
    for ex in examples:
        key = _message_key(ex.messages)
        if key in seen:
            continue
        seen.add(key)
        result.append(ex)
    return result


def _message_key(messages: list[dict]) -> str:
    """Stable hashable representation of a message list."""
    import json

    return json.dumps(messages, sort_keys=True, default=str)


def _limit(examples: list[TrainingExample], max_examples: int) -> list[TrainingExample]:
    """Retain the first ``max_examples`` examples."""
    return examples[:max_examples]
