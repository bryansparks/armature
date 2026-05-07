"""Tests for armature.config — wizard defaults persistence."""
import pytest
from pathlib import Path
from armature import config as cfg_mod


# ── Helpers ────────────────────────────────────────────────────────────────

def _full_defaults():
    return {
        "model_tiers": {
            "small": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "temperature": 0.5},
            "frontier": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
        "role_type_defaults": {
            "worker": "small",
            "judge": "frontier",
            "orchestrator": "frontier",
        },
    }


# ── load ────────────────────────────────────────────────────────────────────

def test_load_returns_empty_when_missing(tmp_path):
    result = cfg_mod.load(tmp_path / "nonexistent.config")
    assert result == {}


def test_load_returns_dict_from_valid_yaml(tmp_path):
    p = tmp_path / "defaults.config"
    p.write_text("model_tiers:\n  small:\n    provider: anthropic\n    model: claude-haiku-4-5-20251001\n")
    result = cfg_mod.load(p)
    assert result["model_tiers"]["small"]["provider"] == "anthropic"


def test_load_handles_empty_yaml_file(tmp_path):
    p = tmp_path / "empty.config"
    p.write_text("")
    result = cfg_mod.load(p)
    assert result == {}


# ── save / round-trip ───────────────────────────────────────────────────────

def test_save_creates_parent_dir(tmp_path):
    p = tmp_path / "subdir" / "defaults.config"
    cfg_mod.save(_full_defaults(), p)
    assert p.exists()


def test_save_round_trip_tiers(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save(_full_defaults(), p)
    loaded = cfg_mod.load(p)
    assert loaded["model_tiers"]["small"]["provider"] == "anthropic"
    assert loaded["model_tiers"]["frontier"]["model"] == "claude-sonnet-4-6"


def test_save_round_trip_role_defaults(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save(_full_defaults(), p)
    loaded = cfg_mod.load(p)
    assert loaded["role_type_defaults"]["worker"] == "small"
    assert loaded["role_type_defaults"]["judge"] == "frontier"


def test_save_includes_temperature(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save(_full_defaults(), p)
    text = p.read_text()
    assert "temperature: 0.5" in text


def test_save_omits_missing_optional_fields(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save({"model_tiers": {"small": {"provider": "anthropic", "model": "m"}}}, p)
    text = p.read_text()
    assert "api_base" not in text
    assert "api_key_env" not in text
    assert "temperature" not in text
    assert "max_tokens" not in text


def test_save_writes_api_base_and_key_env(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save({"model_tiers": {"tiny": {
        "provider": "ollama",
        "model": "qwen2.5:7b",
        "api_base": "http://localhost:11434",
        "api_key_env": "OLLAMA_KEY",
    }}}, p)
    text = p.read_text()
    assert "api_base: http://localhost:11434" in text
    assert "api_key_env: OLLAMA_KEY" in text


def test_save_writes_max_tokens(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save({"model_tiers": {"big": {
        "provider": "anthropic", "model": "x", "max_tokens": 4096
    }}}, p)
    text = p.read_text()
    assert "max_tokens: 4096" in text


def test_save_empty_defaults_produces_header_only(tmp_path):
    p = tmp_path / "defaults.config"
    cfg_mod.save({}, p)
    text = p.read_text()
    assert "Armature wizard defaults" in text
    assert "model_tiers:" not in text
    assert "role_type_defaults:" not in text


def test_save_returns_path(tmp_path):
    p = tmp_path / "defaults.config"
    returned = cfg_mod.save({}, p)
    assert returned == p


# ── tiers_from_config ───────────────────────────────────────────────────────

def test_tiers_from_config_basic():
    config_tiers = {"small": {"provider": "anthropic", "model": "haiku"}}
    result = cfg_mod.tiers_from_config(config_tiers)
    assert len(result) == 1
    assert result[0]["tier"] == "small"
    assert result[0]["provider"] == "anthropic"
    assert result[0]["model"] == "haiku"


def test_tiers_from_config_fills_optional_fields_as_empty_strings():
    result = cfg_mod.tiers_from_config({"t": {"provider": "x", "model": "y"}})
    assert result[0]["api_base"] == ""
    assert result[0]["api_key_env"] == ""
    assert result[0]["temperature"] == ""
    assert result[0]["max_tokens"] == ""


def test_tiers_from_config_preserves_optional_values():
    config_tiers = {"tiny": {
        "provider": "ollama",
        "model": "qwen",
        "api_base": "http://localhost:11434",
        "api_key_env": "KEY",
        "temperature": 0.3,
        "max_tokens": 1024,
    }}
    result = cfg_mod.tiers_from_config(config_tiers)
    assert result[0]["api_base"] == "http://localhost:11434"
    assert result[0]["temperature"] == "0.3"
    assert result[0]["max_tokens"] == "1024"


def test_tiers_from_config_multiple_tiers():
    config_tiers = {
        "small": {"provider": "a", "model": "m1"},
        "frontier": {"provider": "b", "model": "m2"},
    }
    result = cfg_mod.tiers_from_config(config_tiers)
    tier_names = {t["tier"] for t in result}
    assert tier_names == {"small", "frontier"}


# ── tiers_to_config ─────────────────────────────────────────────────────────

def test_tiers_to_config_basic():
    wizard_tiers = [{"tier": "small", "provider": "anthropic", "model": "haiku",
                     "api_base": "", "api_key_env": "", "temperature": "", "max_tokens": ""}]
    result = cfg_mod.tiers_to_config(wizard_tiers)
    assert "small" in result
    assert result["small"]["provider"] == "anthropic"
    assert result["small"]["model"] == "haiku"


def test_tiers_to_config_omits_empty_optional_fields():
    wizard_tiers = [{"tier": "t", "provider": "x", "model": "y",
                     "api_base": "", "api_key_env": "", "temperature": "", "max_tokens": ""}]
    result = cfg_mod.tiers_to_config(wizard_tiers)
    assert "api_base" not in result["t"]
    assert "temperature" not in result["t"]
    assert "max_tokens" not in result["t"]


def test_tiers_to_config_coerces_temperature_to_float():
    wizard_tiers = [{"tier": "t", "provider": "x", "model": "y",
                     "api_base": "", "api_key_env": "", "temperature": "0.7", "max_tokens": ""}]
    result = cfg_mod.tiers_to_config(wizard_tiers)
    assert result["t"]["temperature"] == 0.7
    assert isinstance(result["t"]["temperature"], float)


def test_tiers_to_config_coerces_max_tokens_to_int():
    wizard_tiers = [{"tier": "t", "provider": "x", "model": "y",
                     "api_base": "", "api_key_env": "", "temperature": "", "max_tokens": "2048"}]
    result = cfg_mod.tiers_to_config(wizard_tiers)
    assert result["t"]["max_tokens"] == 2048
    assert isinstance(result["t"]["max_tokens"], int)


def test_tiers_to_config_ignores_invalid_temperature():
    wizard_tiers = [{"tier": "t", "provider": "x", "model": "y",
                     "api_base": "", "api_key_env": "", "temperature": "hot", "max_tokens": ""}]
    result = cfg_mod.tiers_to_config(wizard_tiers)
    assert "temperature" not in result["t"]


def test_tiers_to_config_ignores_invalid_max_tokens():
    wizard_tiers = [{"tier": "t", "provider": "x", "model": "y",
                     "api_base": "", "api_key_env": "", "temperature": "", "max_tokens": "big"}]
    result = cfg_mod.tiers_to_config(wizard_tiers)
    assert "max_tokens" not in result["t"]


def test_tiers_round_trip_from_and_to():
    original = {
        "small": {"provider": "anthropic", "model": "haiku", "temperature": 0.5},
        "frontier": {"provider": "anthropic", "model": "sonnet", "api_base": "http://x"},
    }
    wizard_form = cfg_mod.tiers_from_config(original)
    restored = cfg_mod.tiers_to_config(wizard_form)
    assert restored["small"]["temperature"] == 0.5
    assert restored["frontier"]["api_base"] == "http://x"


# ── summarize ───────────────────────────────────────────────────────────────

def test_summarize_empty_returns_empty_list():
    assert cfg_mod.summarize({}) == []


def test_summarize_tiers_line():
    lines = cfg_mod.summarize(_full_defaults())
    tier_line = next(l for l in lines if "Model tiers" in l)
    assert "small" in tier_line
    assert "anthropic" in tier_line


def test_summarize_role_defaults_line():
    lines = cfg_mod.summarize(_full_defaults())
    rd_line = next(l for l in lines if "Role defaults" in l)
    assert "worker" in rd_line
    assert "small" in rd_line


def test_summarize_only_tiers_no_roles():
    lines = cfg_mod.summarize({"model_tiers": {"s": {"provider": "x", "model": "y"}}})
    assert len(lines) == 1
    assert "Model tiers" in lines[0]
