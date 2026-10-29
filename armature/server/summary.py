"""A workflow told in plain words, for people who don't read YAML.

``describe_workflow`` turns a workflow body into a title, its inputs, and one
sentence per step. ``diff_summaries`` compares two of those. Both are pure and
deterministic, so the same body always reads the same way.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from armature.server import defs

SENTENCE_MAX = 240
STEP_FIELDS = ("label", "kind", "what", "after", "fan_out", "output", "optional", "retry")
WORKFLOW_FIELDS = ("title", "description", "inputs")

_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_BLOCK = re.compile(r"\{%.*?%\}", re.S)
_EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.S)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _placeholder(expression: str) -> str:
    reference = " ".join(expression.split("|", 1)[0].split())
    return f"⟨{reference}⟩" if reference else ""


def readable(text: Any) -> str:
    """Text with Jinja comments and blocks dropped and every {{ x }} shown as ⟨x⟩, on one line."""
    if not isinstance(text, str):
        return ""
    text = _BLOCK.sub("", _COMMENT.sub("", text))
    text = _EXPRESSION.sub(lambda match: _placeholder(match.group(1)), text)
    return " ".join(text.split())


def first_sentence(text: Any) -> str:
    flat = readable(text)
    sentence = _SENTENCE_END.split(flat, 1)[0]
    return sentence if len(sentence) <= SENTENCE_MAX else sentence[:SENTENCE_MAX - 1].rstrip() + "…"


def _reference(source: Any) -> str:
    flat = readable(source)
    return flat if flat.startswith("⟨") else f"⟨{flat}⟩"


def _kind(stage: dict[str, Any]) -> str:
    if stage.get("role") or stage.get("agent"):
        return "llm"
    if stage.get("tool_call"):
        return "tool"
    if stage.get("gate"):
        return "gate"
    if stage.get("subagent_spec"):
        return "subagent"
    if stage.get("adapter"):
        return "script"
    return "other"


def _what(stage: dict[str, Any], kind: str) -> str:
    if kind == "llm":
        role = stage.get("role") if isinstance(stage.get("role"), dict) else {}
        name = readable(role.get("name")) or readable(stage.get("agent")) or "A model"
        sentence = first_sentence(role.get("description"))
        return f"{name}: {sentence}" if sentence else name
    if kind == "tool":
        tool = stage["tool_call"] if isinstance(stage["tool_call"], dict) else {}
        return f"Calls tool {readable(tool.get('name')) or 'unnamed'}"
    if kind == "gate":
        return "Waits for human approval"
    if kind == "subagent":
        return f"Starts workflow {readable(stage.get('subagent_spec'))}"
    if kind == "script":
        return f"Runs script {readable(stage.get('adapter'))}"
    return "Does nothing yet"


def _label(stage: dict[str, Any], step_id: str) -> str:
    role = stage.get("role")
    if isinstance(role, dict) and readable(role.get("name")):
        return readable(role.get("name"))
    words = re.sub(r"[-_]+", " ", step_id).strip()
    return words[:1].upper() + words[1:] if words else step_id


def _output(stage: dict[str, Any]) -> str:
    if stage.get("output_mode") != "guided_json":
        return "text"
    schema = stage.get("output_schema") if isinstance(stage.get("output_schema"), dict) else {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    fields = [str(name) for name in (required or list(properties))]
    return f"structured: {', '.join(fields)}" if fields else "structured"


def _retry(stage: dict[str, Any], step_id: str) -> str | None:
    on_fail = stage.get("on_fail") if isinstance(stage.get("on_fail"), dict) else {}
    loop = on_fail.get("loop") if isinstance(on_fail.get("loop"), dict) else None
    if loop is None:
        return None
    times = loop.get("max", 3)
    target = str(loop.get("stage") or step_id)
    if target == step_id:
        return f"Tries again up to {times} times if it fails"
    return f"If it fails, goes back to {target}, up to {times} times"


def _fingerprint(stage: dict[str, Any]) -> str:
    text = json.dumps(stage, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def describe_step(stage: Any, index: int) -> dict[str, Any]:
    stage = stage if isinstance(stage, dict) else {}
    step_id = str(stage.get("id") or f"step-{index + 1}")
    kind = _kind(stage)
    after = stage.get("depends_on") if isinstance(stage.get("depends_on"), list) else []
    step: dict[str, Any] = {"id": step_id, "label": _label(stage, step_id), "kind": kind,
                            "what": _what(stage, kind), "after": [str(item) for item in after],
                            "output": _output(stage), "fan_out": None, "optional": stage.get("fail_as_value") is True,
                            "retry": _retry(stage, step_id), "fingerprint": _fingerprint(stage)}
    if stage.get("fan_out"):
        step["fan_out"] = (f"for each item in {_reference(stage.get('partition_source'))}, "
                           f"up to {stage['fan_out']} at a time")
    return step


def _inputs(data: dict[str, Any]) -> list[dict[str, Any]]:
    contracts = data.get("contracts") if isinstance(data.get("contracts"), dict) else {}
    listed = contracts.get("inputs") if isinstance(contracts.get("inputs"), list) else []
    inputs = []
    for item in listed:
        if isinstance(item, str):
            inputs.append({"name": item})
        elif isinstance(item, dict) and item.get("name"):
            entry: dict[str, Any] = {"name": str(item["name"])}
            if isinstance(item.get("required"), bool):
                entry["required"] = item["required"]
            inputs.append(entry)
    return inputs


def describe_workflow(body: Any) -> dict[str, Any]:
    """The title, inputs, steps, and post-run steps of a workflow body, or an error when it is not a workflow."""
    data = body if isinstance(body, dict) else defs._quiet_load(body if isinstance(body, str) else None)
    if not isinstance(data, dict):
        return {"title": None, "description": "", "inputs": [], "steps": [], "post_run": [],
                "error": "The YAML could not be read as a workflow."}
    stages = data.get("stages") if isinstance(data.get("stages"), list) else []
    steps, post_run = [], []
    for index, stage in enumerate(stages):
        described = describe_step(stage, index)
        (post_run if isinstance(stage, dict) and stage.get("post_run") is True else steps).append(described)
    return {"title": readable(data.get("name")) or None, "description": readable(data.get("description")),
            "inputs": _inputs(data), "steps": steps, "post_run": post_run, "error": None}


def _all_steps(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["id"]: step for step in [*summary.get("steps", []), *summary.get("post_run", [])]}


def _same_apart_from_id(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return all(first.get(field) == second.get(field) for field in STEP_FIELDS if field != "label")


def diff_summaries(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Steps added, removed, changed (with the fields that differ), and renamed, from two describe_workflow results."""
    before, after = _all_steps(old), _all_steps(new)
    added = [step_id for step_id in after if step_id not in before]
    removed = [step_id for step_id in before if step_id not in after]
    renamed = []
    for old_id in list(removed):
        match = next((new_id for new_id in added if _same_apart_from_id(before[old_id], after[new_id])), None)
        if match is not None:
            renamed.append({"from": old_id, "to": match})
            removed.remove(old_id)
            added.remove(match)
    changed = []
    for step_id in after:
        if step_id not in before:
            continue
        fields = [field for field in STEP_FIELDS if before[step_id].get(field) != after[step_id].get(field)]
        if not fields and before[step_id].get("fingerprint") != after[step_id].get("fingerprint"):
            fields = ["details"]
        if fields:
            changed.append({"id": step_id, "fields": fields})
    return {"added": added, "removed": removed, "changed": changed, "renamed": renamed,
            "workflow": [field for field in WORKFLOW_FIELDS if old.get(field) != new.get(field)]}
