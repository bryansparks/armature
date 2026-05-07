"""Persistent wizard defaults stored at ~/.armature/defaults.config.

The file is YAML and is intended to be human-editable. It stores the
sections the wizard can skip when defaults are accepted:

  model_tiers:
    small:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      temperature: 0.5
    frontier:
      provider: anthropic
      model: claude-sonnet-4-6

  role_type_defaults:
    worker: small
    judge: frontier
    orchestrator: frontier
    researcher: large

Only keys present in the file override wizard prompts. Missing keys fall
back to wizard interaction normally.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.armature/defaults.config").expanduser()

_FILE_HEADER = """\
# Armature wizard defaults
# ─────────────────────────────────────────────────────────────────
# Edit this file to pre-fill common wizard prompts.
# Run: armature new   to use these defaults interactively.
# Sections omitted here are always prompted in the wizard.
# ─────────────────────────────────────────────────────────────────

"""


# ── I/O ────────────────────────────────────────────────────────────────────

def load(path: Path | str | None = None) -> dict[str, Any]:
    """Load defaults file. Returns empty dict if the file doesn't exist."""
    p = Path(path or DEFAULT_CONFIG_PATH)
    if not p.exists():
        return {}
    from ruamel.yaml import YAML
    yaml = YAML()
    data = yaml.load(p.read_text(encoding="utf-8"))
    return dict(data) if data else {}


def save(defaults: dict[str, Any], path: Path | str | None = None) -> Path:
    """Write defaults to the config file. Returns the path written."""
    p = Path(path or DEFAULT_CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)

    lines = [_FILE_HEADER]

    tiers = defaults.get("model_tiers", {})
    if tiers:
        lines.append("model_tiers:")
        for tier_name, cfg in tiers.items():
            lines.append(f"  {tier_name}:")
            lines.append(f"    provider: {cfg['provider']}")
            lines.append(f"    model: {cfg['model']}")
            if cfg.get("api_base"):
                lines.append(f"    api_base: {cfg['api_base']}")
            if cfg.get("api_key_env"):
                lines.append(f"    api_key_env: {cfg['api_key_env']}")
            if cfg.get("temperature") not in (None, ""):
                lines.append(f"    temperature: {cfg['temperature']}")
            if cfg.get("max_tokens") not in (None, ""):
                lines.append(f"    max_tokens: {cfg['max_tokens']}")
        lines.append("")

    role_defaults = defaults.get("role_type_defaults", {})
    if role_defaults:
        lines.append("role_type_defaults:")
        for role, tier in role_defaults.items():
            lines.append(f"  {role}: {tier}")
        lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ── Tier format converters ──────────────────────────────────────────────────
# The wizard works with a list[dict] internally; the config file uses a
# tier-name-keyed dict. These two functions convert between the formats.

def tiers_from_config(config_tiers: dict) -> list[dict]:
    """Convert config file tier dict → wizard internal tier list."""
    result = []
    for tier_name, cfg in config_tiers.items():
        result.append({
            "tier": tier_name,
            "provider": cfg.get("provider", ""),
            "model": cfg.get("model", ""),
            "api_base": str(cfg.get("api_base", "") or ""),
            "api_key_env": str(cfg.get("api_key_env", "") or ""),
            "temperature": str(cfg.get("temperature", "") or ""),
            "max_tokens": str(cfg.get("max_tokens", "") or ""),
        })
    return result


def tiers_to_config(wizard_tiers: list[dict]) -> dict:
    """Convert wizard internal tier list → config file tier dict."""
    result = {}
    for t in wizard_tiers:
        cfg: dict[str, Any] = {
            "provider": t["provider"],
            "model": t["model"],
        }
        if t.get("api_base"):
            cfg["api_base"] = t["api_base"]
        if t.get("api_key_env"):
            cfg["api_key_env"] = t["api_key_env"]
        if t.get("temperature") not in (None, ""):
            try:
                cfg["temperature"] = float(t["temperature"])
            except (ValueError, TypeError):
                pass
        if t.get("max_tokens") not in (None, ""):
            try:
                cfg["max_tokens"] = int(t["max_tokens"])
            except (ValueError, TypeError):
                pass
        result[t["tier"]] = cfg
    return result


# ── Summary helper ─────────────────────────────────────────────────────────

def summarize(defaults: dict[str, Any]) -> list[str]:
    """Return human-readable bullet points describing the loaded defaults."""
    lines = []
    tiers = defaults.get("model_tiers", {})
    if tiers:
        tier_parts = [f"{name} ({cfg.get('provider','?')}/{cfg.get('model','?')})"
                      for name, cfg in tiers.items()]
        lines.append(f"  Model tiers: {', '.join(tier_parts)}")
    role_defaults = defaults.get("role_type_defaults", {})
    if role_defaults:
        rd_parts = [f"{r}→{t}" for r, t in role_defaults.items()]
        lines.append(f"  Role defaults: {', '.join(rd_parts)}")
    return lines
