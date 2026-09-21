"""Spec-level smoke tests for the TypeSafe decision A/B example."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from armature.spec.loader import load_spec
from armature.spec.validator import validate_spec

_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "decision-typesafe"


@pytest.fixture
def spec_path() -> Path:
    return _EXAMPLE_DIR / "ab-demo.yml"


def test_ab_demo_spec_loads(spec_path):
    spec = load_spec(spec_path)
    assert spec.name == "typesafe-decision-ab"
    assert len(spec.stages) == 4
    assert [s.id for s in spec.stages] == ["load_benchmark", "decide", "llm_judge", "compare"]


def test_ab_demo_spec_validates(spec_path):
    spec = load_spec(spec_path)
    errors = validate_spec(spec, strict=False)
    assert not any(e.severity == "error" for e in errors)


def test_script_adapters_use_parse_json(spec_path):
    spec = load_spec(spec_path)
    adapters = spec.adapters
    for name in ("load_benchmark", "decide", "compare"):
        assert name in adapters, f"missing adapter {name}"
        assert adapters[name].parse == "json"
        assert adapters[name].cmd


def test_fan_out_stages_partition_over_benchmark_states(spec_path):
    spec = load_spec(spec_path)
    stages = {s.id: s for s in spec.stages}
    for sid in ("decide", "llm_judge"):
        stage = stages[sid]
        assert stage.partition_source == "{{ load_benchmark.states }}"
        assert stage.partition_key == "state_item"
        assert stage.fan_in == "list"


def test_benchmark_has_labeled_states():
    benchmark = json.loads((_EXAMPLE_DIR / "benchmark.json").read_text())
    states = benchmark["states"]
    assert len(states) == 20
    ids = [s["id"] for s in states]
    assert len(set(ids)) == len(ids), "duplicate state ids"
    for state in states:
        assert state["text"].strip(), f"{state['id']} has empty text"
        labels = state["labels"]
        assert isinstance(labels["is_concrete"], bool)
        assert labels["severity"] in ("low", "medium", "high")
        assert labels["actionability"] in (0, 1, 2)


def test_questions_battery_matches_judge_dimensions():
    questions = json.loads((_EXAMPLE_DIR / "questions.json").read_text())
    assert set(questions) == {"is_concrete", "severity", "actionability"}
    assert questions["is_concrete"]["type"] == "noul"
    assert questions["severity"]["type"] == "choice"
    assert set(questions["severity"]["criteria"]) == {"low", "medium", "high"}
    assert questions["actionability"]["type"] == "score"
    assert len(questions["actionability"]["criteria"]) == 3
