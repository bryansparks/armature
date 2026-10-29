"""Typed workflow inputs and outputs declared in ``contracts:``.

``contracts.inputs`` entries stay plain dicts on the model so untyped specs
behave exactly as before. An entry that sets ``type``, ``default`` or ``enum``
is checked here: the engine coerces CLI and server strings to the declared
type before anything renders, applies defaults, and enforces enum and
required. ``contracts.outputs`` entries with a ``name`` are typed outputs
projected from the run's results with a ``from:`` template.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from jinja2 import ChainableUndefined, Undefined

INPUT_TYPES = ("string", "boolean", "integer", "number", "object", "array")
SCALAR_TYPES = ("string", "boolean", "integer", "number")

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")
_INT_RE = re.compile(r"[+-]?\d+")


class InputError(ValueError):
    """A runtime input that is missing, of the wrong type, or outside its enum."""


def is_typed(decl: dict[str, Any]) -> bool:
    return any(k in decl for k in ("type", "default", "enum"))


def matches_type(type_: str, value: Any) -> bool:
    """Whether ``value`` already is ``type_`` without coercion (YAML-native check)."""
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "object":
        return isinstance(value, dict)
    if type_ == "array":
        return isinstance(value, list)
    return True


def coerce(type_: str | None, value: Any, label: str) -> Any:
    """``value`` as ``type_``, converting the strings a CLI or form sends. Raises InputError."""
    if type_ is None or value is None or matches_type(type_, value):
        return value
    if type_ == "string":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
    elif type_ == "boolean":
        text = str(value).strip().lower() if isinstance(value, (str, int)) else None
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    elif type_ == "integer":
        if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
            return int(value.strip())
        if isinstance(value, float) and value.is_integer():
            return int(value)
    elif type_ == "number":
        if isinstance(value, str):
            text = value.strip()
            if _INT_RE.fullmatch(text):
                return int(text)
            try:
                return float(text)
            except ValueError:
                pass
    elif type_ in ("object", "array") and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if matches_type(type_, parsed):
            return parsed
    raise InputError(f"{label} must be {_article(type_)}, got {value!r}")


def _article(type_: str) -> str:
    return f"an {type_}" if type_[0] in "aeiou" else f"a {type_}"


def declaration_errors(decls: list[dict[str, Any]], where: str) -> list[tuple[str, str]]:
    """(code, message) per bad typed declaration: an unknown type, or a default or enum of the wrong type."""
    problems: list[tuple[str, str]] = []
    for i, decl in enumerate(decls):
        label = f"{where}[{i}]" + (f" '{decl['name']}'" if "name" in decl else "")
        type_ = decl.get("type")
        if type_ is not None and type_ not in INPUT_TYPES:
            problems.append(("CONTRACT_INVALID_TYPE",
                             f"{label}: type '{type_}' is not one of {list(INPUT_TYPES)}"))
            continue
        enum = decl.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or not enum:
                problems.append(("CONTRACT_INVALID_ENUM", f"{label}: enum must be a non-empty list"))
                enum = None
            elif type_ is not None:
                bad = [v for v in enum if not matches_type(type_, v)]
                if bad:
                    problems.append(("CONTRACT_INVALID_ENUM", f"{label}: enum values {bad!r} are not {type_}"))
        if decl.get("default") is not None:
            default = decl["default"]
            if type_ is not None and not matches_type(type_, default):
                problems.append(("CONTRACT_INVALID_DEFAULT",
                                 f"{label}: default {default!r} is not {_article(type_)}"))
            elif enum is not None and default not in enum:
                problems.append(("CONTRACT_INVALID_DEFAULT",
                                 f"{label}: default {default!r} is not in enum {enum!r}"))
    return problems


def resolve_inputs(decls: list[dict[str, Any]], values: dict[str, Any], *,
                   enforce_required: bool = True, scalars_only: bool = False) -> dict[str, Any]:
    """``values`` with declared inputs coerced, defaults applied, and enum and required enforced.

    Keys nobody declared pass through untouched. An empty string for a typed,
    non-string input counts as missing. ``scalars_only`` skips object and array
    inputs, for load-time rendering into YAML text.
    """
    out = dict(values)
    problems: list[str] = []
    for decl in decls:
        name = decl.get("name")
        if name is None:
            continue
        type_ = decl.get("type")
        if scalars_only and type_ is not None and type_ not in SCALAR_TYPES:
            continue
        if type_ not in (None, "string") and isinstance(out.get(name), str) and not out[name].strip():
            out.pop(name)
        if out.get(name) is None and decl.get("default") is not None:
            out[name] = copy.deepcopy(decl["default"])
        value = out.get(name)
        if value is None:
            if enforce_required and decl.get("required", False):
                problems.append(f"Required input '{name}' missing from context")
            continue
        try:
            value = coerce(type_, value, f"Input '{name}'")
        except InputError as exc:
            problems.append(str(exc))
            continue
        enum = decl.get("enum")
        if isinstance(enum, list) and value not in enum:
            problems.append(f"Input '{name}' must be one of {enum!r}, got {value!r}")
            continue
        out[name] = value
    if problems:
        raise InputError("; ".join(problems))
    return out


def typed_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The typed entries of ``contracts.outputs`` (the ones with a ``name``)."""
    return [o for o in outputs if "name" in o]


def project_outputs(outputs: list[dict[str, Any]], results: dict[str, Any]) -> dict[str, Any]:
    """Render each typed output's ``from:`` against ``results`` and coerce it to its type."""
    from armature.runtime.templating import native_env

    env = native_env(ChainableUndefined)
    projected: dict[str, Any] = {}
    for out in typed_outputs(outputs):
        value = env.from_string(str(out.get("from", ""))).render(**results)
        if isinstance(value, Undefined):
            value = None
        projected[out["name"]] = coerce(out.get("type"), value, f"Output '{out['name']}'")
    return projected
