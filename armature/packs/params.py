"""A step's ``with:`` values: schema checks and run-time rendering."""
from __future__ import annotations

import re
from typing import Any

from jinja2 import StrictUndefined, Undefined
from jsonschema import Draft202012Validator

from armature.runtime.templating import native_env, text_env

_MARKERS = ("{{", "{%", "{#")
_LONE_REF = re.compile(r"\s*\{\{((?:(?!\{\{|\}\}).)*)\}\}\s*", re.S)


def is_template(value: Any) -> bool:
    return isinstance(value, str) and any(marker in value for marker in _MARKERS)


def _where(path) -> str:
    out = "with"
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def with_errors(schema: dict[str, Any], values: Any, *, allow_templates: bool) -> list[str]:
    """One message per schema violation in ``values``, each prefixed with its field path.

    With ``allow_templates`` a violation whose value is still a template is skipped,
    because its type is only known once it renders.
    """
    return [message for _, message in with_error_paths(schema, values, allow_templates=allow_templates)]


def with_error_paths(schema: dict[str, Any], values: Any, *,
                     allow_templates: bool) -> list[tuple[list[str | int], str]]:
    """``with_errors``, with each message's path inside ``with:`` as a list of keys and indexes."""
    found = []
    for err in sorted(Draft202012Validator(schema).iter_errors(values), key=lambda e: list(e.absolute_path)):
        if allow_templates and is_template(err.instance):
            continue
        found.append((list(err.absolute_path), f"{_where(err.absolute_path)}: {err.message}"))
    return found


def template_strings(values: Any, path: tuple = ()) -> list[tuple[str, str]]:
    """Every templated string in ``values`` with its field path."""
    if is_template(values):
        return [(_where(path), values)]
    if isinstance(values, dict):
        return [item for key, value in values.items() for item in template_strings(value, (*path, key))]
    if isinstance(values, list):
        return [item for i, value in enumerate(values) for item in template_strings(value, (*path, i))]
    return []


def render_template(value: str, context: dict[str, Any], *, native: bool) -> Any:
    """Render one templated string. A lone reference keeps its value as is, so a string stays a string."""
    lone = _LONE_REF.fullmatch(value)
    if lone is None:
        env = native_env if native else text_env
        return env(StrictUndefined).from_string(value).render(**context)
    result = native_env(StrictUndefined).compile_expression(lone.group(1), undefined_to_none=False)(**context)
    if isinstance(result, Undefined):
        str(result)
    return result


def render_with(values: Any, context: dict[str, Any]) -> Any:
    """Render each templated string in the sandbox, keeping a lone reference's value and type."""
    if is_template(values):
        return render_template(values, context, native=True)
    if isinstance(values, dict):
        return {key: render_with(value, context) for key, value in values.items()}
    if isinstance(values, list):
        return [render_with(value, context) for value in values]
    return values
