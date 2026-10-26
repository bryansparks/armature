"""Helpers that write small packs for the pack tests."""
from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
HELLO = ROOT / "examples" / "packs" / "hello"


def write_pack(directory: Path, name: str = "demo", version: str = "1.0.0", **adapters) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "version": version, "adapters": adapters or {
        "echo": {"cmd": "echo '{}'", "params": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    }}
    with (directory / "pack.yaml").open("w", encoding="utf-8") as fh:
        YAML(typ="safe").dump(body, fh)
    return directory
