"""Lint a spec's templates down to a safe subset of Jinja2.

The sandbox in armature.runtime.templating stops a template from reaching
Python internals. This lint is narrower: a stored workflow's templates may
only reference inputs and step outputs, compare them, and pass them through a
few formatting filters. It is opt-in, for workflows edited in the browser and
for ``armature validate --strict-templates``.

Names and attributes starting with ``_`` are refused, except the name
``_result``, which the engine binds for a loop ``until`` when a stage result
is not a dict.

``load_spec(path, vars)`` and ``load_child_spec`` render every string scalar
of the spec, not only the fields the engine renders at run time, so
``lint_tree`` walks every string in the parsed YAML.
"""
from __future__ import annotations

from typing import Any, Iterator

from jinja2 import nodes
from jinja2.exceptions import TemplateSyntaxError

from armature.runtime.templating import text_env
from armature.spec.models import HarnessSpec
from armature.spec.validator import SpecError

ALLOWED_FILTERS = frozenset({"default", "length", "lower", "upper", "trim", "int", "string", "tojson"})
ALLOWED_PRIVATE_NAMES = frozenset({"_result", "_iteration"})

_ALLOWED_NODES = (
    nodes.Template,
    nodes.Output,
    nodes.Const,
    nodes.List,
    nodes.Tuple,
    nodes.Dict,
    nodes.Pair,
    nodes.Keyword,
    nodes.Compare,
    nodes.Operand,
    nodes.And,
    nodes.Or,
    nodes.Not,
    nodes.CondExpr,
)

_MARKERS = ("{{", "{%", "{#")


def lint_template(text: str) -> list[str]:
    """Return one message per construct in ``text`` outside the safe subset."""
    try:
        tree = text_env().parse(text)
    except TemplateSyntaxError as exc:
        return [f"line {exc.lineno}: template syntax error: {exc.message}"]
    errors: list[str] = []
    _check(tree, errors)
    return errors


def _check(node: nodes.Node, errors: list[str]) -> None:
    where = f"line {node.lineno}: " if getattr(node, "lineno", None) else ""
    if isinstance(node, nodes.TemplateData):
        # A {% raw %} block parses as plain text but renders back into a template at load time.
        if any(marker in node.data for marker in _MARKERS):
            errors.append(f"{where}template markers inside text ({{% raw %}}) are not allowed")
    elif isinstance(node, nodes.Name):
        if node.name.startswith("_") and node.name not in ALLOWED_PRIVATE_NAMES:
            errors.append(f"{where}name '{node.name}' starts with '_'")
    elif isinstance(node, nodes.Getattr):
        if node.attr.startswith("_"):
            errors.append(f"{where}attribute '{node.attr}' starts with '_'")
    elif isinstance(node, nodes.Getitem):
        if not isinstance(node.arg, nodes.Const):
            errors.append(f"{where}an index must be a constant")
            return
        if isinstance(node.arg.value, str) and node.arg.value.startswith("_"):
            errors.append(f"{where}key '{node.arg.value}' starts with '_'")
    elif isinstance(node, nodes.Filter):
        if node.name not in ALLOWED_FILTERS:
            allowed = ", ".join(sorted(ALLOWED_FILTERS))
            errors.append(f"{where}filter '{node.name}' is not allowed (allowed: {allowed})")
    elif isinstance(node, _ALLOWED_NODES):
        pass
    elif isinstance(node, nodes.Call):
        errors.append(f"{where}calls are not allowed")
        return
    elif isinstance(node, nodes.Test):
        errors.append(f"{where}tests ('is {node.name}') are not allowed")
        return
    elif isinstance(node, nodes.Stmt):
        errors.append(f"{where}statements ({{% {type(node).__name__.lower()} %}}) are not allowed")
        return
    else:
        errors.append(f"{where}{type(node).__name__} expressions are not allowed")
        return
    for child in node.iter_child_nodes():
        _check(child, errors)


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}[{i}]")


def _stage_id(data: Any, path: str) -> str | None:
    if not isinstance(data, dict) or not path.startswith("stages["):
        return None
    stages = data.get("stages")
    index = int(path[len("stages["):path.index("]")])
    if isinstance(stages, list) and index < len(stages) and isinstance(stages[index], dict):
        stage_id = stages[index].get("id")
        return str(stage_id) if stage_id is not None else None
    return None


def lint_tree(data: Any) -> list[SpecError]:
    """Lint every string scalar of a parsed spec tree, as the loader renders them."""
    errors: list[SpecError] = []
    for path, text in _strings(data, ""):
        if not any(marker in text for marker in _MARKERS):
            continue
        for message in lint_template(text):
            errors.append(SpecError(
                code="UNSAFE_TEMPLATE", message=f"{path}: {message}", stage_id=_stage_id(data, path),
            ))
    return errors


def lint_spec(spec: HarnessSpec) -> list[SpecError]:
    """Lint every string field of ``spec``. Not part of validate_spec."""
    return lint_tree(spec.model_dump(mode="json"))
