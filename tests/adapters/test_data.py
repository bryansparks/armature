"""Tests for adapter training-data primitives."""
from __future__ import annotations

import json

from armature.adapters.data import TrainingDataset, TrainingExample


def test_save_jsonl(tmp_path):
    dataset = TrainingDataset(
        examples=[
            TrainingExample(
                messages=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                skill_id="greet",
                source="skill",
                score=0.9,
            )
        ],
        base_model="qwen/qwen2.5-7b",
        name="greet-adapter",
    )
    path = tmp_path / "train.jsonl"
    dataset.save_jsonl(path)
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["skill_id"] == "greet"
    assert row["source"] == "skill"
    assert row["score"] == 0.9
    assert row["messages"][0]["content"] == "hello"
