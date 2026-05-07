"""Tests for the wizard's YAML generator (non-interactive path)."""
import pytest
from armature.cli_wizard import generate_yaml, _slug


# ── _slug ──────────────────────────────────────────────────────────────────

def test_slug_lowercases():
    assert _slug("My Stage") == "my_stage"

def test_slug_replaces_hyphens():
    assert _slug("my-stage-id") == "my_stage_id"

def test_slug_already_clean():
    assert _slug("researcher") == "researcher"


# ── generate_yaml ──────────────────────────────────────────────────────────

def _minimal_args(**overrides):
    args = dict(
        meta={"name": "test_wf", "description": "", "version": "1.0"},
        tiers=[{"tier": "small", "provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                "api_base": "", "api_key_env": "", "temperature": "", "max_tokens": ""}],
        role_defaults={},
        stages=[{
            "id": "worker",
            "role": {"name": "Worker", "type": "worker", "description": "Do the work."},
            "output_mode": "text",
            "depends_on": [],
        }],
        adapters=[],
        safety_rules=[],
        memory=None,
    )
    args.update(overrides)
    return args


def test_name_in_output():
    yaml = generate_yaml(**_minimal_args())
    assert "name: test_wf" in yaml


def test_tier_in_output():
    yaml = generate_yaml(**_minimal_args())
    assert "small:" in yaml
    assert "provider: anthropic" in yaml
    assert "claude-haiku-4-5-20251001" in yaml


def test_stage_id_in_output():
    yaml = generate_yaml(**_minimal_args())
    assert "- id: worker" in yaml


def test_no_adapters_section_when_empty():
    yaml = generate_yaml(**_minimal_args())
    assert "adapters:" not in yaml


def test_adapter_section_present_when_set():
    args = _minimal_args(
        adapters=[{"name": "run_cmd", "type": "script", "cmd": "echo hi", "timeout": 30}],
        stages=[{"id": "s1", "adapter": "run_cmd", "depends_on": []}],
    )
    yaml = generate_yaml(**args)
    assert "adapters:" in yaml
    assert "run_cmd:" in yaml
    assert "echo hi" in yaml


def test_safety_rules_section():
    args = _minimal_args(
        adapters=[{"name": "run_cmd", "type": "script", "cmd": "echo hi", "timeout": 30}],
        stages=[{"id": "s1", "adapter": "run_cmd", "depends_on": []}],
        safety_rules=[{
            "tool": "run_cmd",
            "condition": {"field": "cmd", "op": "contains", "value": "rm -rf"},
            "action": "block",
            "message": "blocked",
        }],
    )
    yaml = generate_yaml(**args)
    assert "safety_rules:" in yaml
    assert "action: block" in yaml
    assert "rm -rf" in yaml


def test_memory_section():
    args = _minimal_args(memory={
        "enabled": True,
        "capture": [{"stage": "worker", "key": "content", "max_entries": 5}],
        "inject_as": "_memory",
    })
    yaml = generate_yaml(**args)
    assert "memory:" in yaml
    assert "inject_as: _memory" in yaml
    assert "stage: worker" in yaml


def test_signature_input_in_stage():
    args = _minimal_args(stages=[{
        "id": "analyst",
        "role": {"name": "Analyst", "type": "worker", "description": "Analyze."},
        "output_mode": "json",
        "depends_on": [],
        "signature": {"input": {"topic": "The topic", "research": "Research output"}},
    }])
    yaml = generate_yaml(**args)
    assert "signature:" in yaml
    assert "topic: The topic" in yaml


def test_on_fail_loop_in_stage():
    args = _minimal_args(stages=[{
        "id": "extractor",
        "role": {"name": "Extractor", "type": "worker", "description": "Extract data."},
        "output_mode": "guided_json",
        "depends_on": [],
        "on_fail": {"loop": {"stage": "extractor", "max": 2}},
    }])
    yaml = generate_yaml(**args)
    assert "on_fail:" in yaml
    assert "max: 2" in yaml


def test_human_gate_stage():
    args = _minimal_args(stages=[
        {"id": "s1", "role": {"name": "W", "type": "worker", "description": "work."},
         "output_mode": "text", "depends_on": []},
        {"id": "approval", "gate": "human", "present": "Please review.", "depends_on": ["s1"]},
    ])
    yaml = generate_yaml(**args)
    assert "gate: human" in yaml
    assert "Please review." in yaml


def test_subagent_with_fan_out():
    args = _minimal_args(stages=[{
        "id": "parallel",
        "subagent_spec": "workflows/child.yml",
        "fan_out": 4,
        "fan_in": "merge",
        "partition_key": "items",
        "depends_on": [],
    }])
    yaml = generate_yaml(**args)
    assert "subagent_spec: workflows/child.yml" in yaml
    assert "fan_out: 4" in yaml
    assert "fan_in: merge" in yaml
    assert "partition_key: items" in yaml


def test_tier_with_api_base_and_key_env():
    args = _minimal_args(tiers=[{
        "tier": "tiny",
        "provider": "ollama",
        "model": "qwen2.5:7b",
        "api_base": "http://localhost:11434",
        "api_key_env": "OLLAMA_API_KEY",
        "temperature": "0.3",
        "max_tokens": "1024",
    }])
    yaml = generate_yaml(**args)
    assert "api_base: http://localhost:11434" in yaml
    assert "api_key_env: OLLAMA_API_KEY" in yaml
    assert "temperature: 0.3" in yaml
    assert "max_tokens: 1024" in yaml


def test_role_type_defaults_section():
    args = _minimal_args(role_defaults={
        "worker": "small", "judge": "frontier", "researcher": "large", "orchestrator": "frontier"
    })
    yaml = generate_yaml(**args)
    assert "role_type_defaults:" in yaml
    assert "judge: frontier" in yaml


def test_depends_on_listed():
    args = _minimal_args(stages=[
        {"id": "s1", "role": {"name": "A", "type": "worker", "description": "a."},
         "output_mode": "text", "depends_on": []},
        {"id": "s2", "role": {"name": "B", "type": "worker", "description": "b."},
         "output_mode": "text", "depends_on": ["s1"]},
    ])
    yaml = generate_yaml(**args)
    assert "depends_on: [s1]" in yaml


def test_generated_yaml_is_valid_spec(tmp_path):
    """Generated YAML must load as a valid HarnessSpec."""
    from armature.spec.loader import load_spec
    args = _minimal_args()
    yaml_text = generate_yaml(**args)
    spec_file = tmp_path / "test.yml"
    spec_file.write_text(yaml_text)
    spec = load_spec(spec_file)
    assert spec.name == "test_wf"
    assert len(spec.stages) == 1
