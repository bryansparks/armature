# armature/packaging/refs.py
"""Collect file references from a spec YAML without model validation.

The package builder and verifier walk child specs recursively to vendor and
check their file references (subagent_spec children, context-layer src files).
Children are walked raw — no HarnessSpec validation — because a child spec may
only be model-valid once the run-time context renders its load-time templates;
requiring inert validation here would reject specs that run fine.
"""
from __future__ import annotations
from pathlib import Path
from ruamel.yaml import YAML

_Y = YAML()


def walk_spec_file_refs(spec_file: Path) -> tuple[list[str], list[str]]:
    """Return (subagent_refs, layer_srcs) referenced by a spec YAML file.

    Only refs that are plain strings are collected; anything else (or a
    malformed tree) is ignored — the spec's own load remains the authority
    on validity.
    """
    data = _Y.load(spec_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return [], []
    subagent_refs = [
        s["subagent_spec"] for s in (data.get("stages") or [])
        if isinstance(s, dict) and isinstance(s.get("subagent_spec"), str) and s["subagent_spec"]
    ]
    layer_srcs = [
        l["src"] for l in (data.get("context_layers") or [])
        if isinstance(l, dict) and isinstance(l.get("src"), str) and l["src"]
    ]
    return subagent_refs, layer_srcs