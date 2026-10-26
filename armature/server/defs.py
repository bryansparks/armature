"""Workflows, documents, providers, and settings: scope, permissions, validation, and versioned writes.

A workflow or document belongs to the instance, a team, or a project. A run
resolves a name project first, then team, then instance. Admins write
anywhere, team leads write their teams' and those teams' projects'
definitions, and project leads write their projects' definitions. Every save
is validated, appends a ``versions`` row, and records an audit row.

A workflow is code, so a non-admin may change only an allowlist of fields:
the description, the mission, and each stage's wiring to installed pack
adapters. Everything else must stay as an admin left it.
"""
from __future__ import annotations

import difflib
import io
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from jinja2 import nodes
from jinja2.exceptions import TemplateSyntaxError
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from sqlalchemy import Connection, Table, delete, insert, select, update

from armature.packs.params import with_error_paths
from armature.runtime.templating import text_env
from armature.server import audit, display, packs, rbac
from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import (
    DOCUMENT_KINDS,
    INSTANCE_SCOPE,
    PROVIDER_KINDS,
    documents,
    jobs,
    projects,
    providers,
    runs,
    scope_of,
    secrets,
    settings,
    teams,
    triggers,
    users,
    versions,
    workflows,
)
from armature.spec.models import HarnessSpec
from armature.spec.template_lint import lint_tree
from armature.spec.validator import validate_spec

BODY_MAX = 256 * 1024
NOTE_MAX = 2000
WORKFLOW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DOCUMENT_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
SCOPE_KINDS = ("instance", "team", "project")
INSTANCE_IDS = (None, "", INSTANCE_SCOPE, "instance")

# What a non-admin may change in a workflow. Every other path must match the saved version.
TOP_MUTABLE = ("name", "description", "mission")
STAGE_MUTABLE = ("id", "adapter", "depends_on", "with", "fan_out", "fan_in", "partition_source", "partition_key",
                 "skip_if", "condition", "fail_as_value", "timeout_s", "output_max_chars")
ROLE_MUTABLE = ("description", "model_tier", "model")
TIMEOUT_MAX = 7200
NODE_MAX = 20_000
TEMPLATE_MARKER = re.compile(r"\{\{|\{%|\{#")
GUARDED_FRONT_MATTER = {"tools": "tools_allowlist", "mcp_servers": "mcp_allowlist", "mcp_inherit": "mcp_allowlist"}
# The loader fills these from a pack; one written into a stored workflow would point a step at any directory.
RUNTIME_ADAPTER_FIELDS = ("pack_dir", "params")
# validate_spec expects the loader to have merged pack adapters in, so _pack_refs checks these against the catalog.
# Its UNSAFE_TEMPLATE findings are skipped too, because _lint covers every string and knows the saved version.
PACK_REF_CODES = ("UNDEFINED_ADAPTER", "UNKNOWN_PACK_ADAPTER")
ACTIVE_JOBS = ("queued", "claimed", "running")

# Fields of a stage the form editor reads and writes, with the value that means absent.
STAGE_FIELDS = {"adapter": None, "depends_on": [], "with": {}, "fan_out": None, "partition_source": None,
                "fail_as_value": False, "skip_if": None}

ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,190}$")
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
API_BASE = re.compile(r"^https?://[^\s/?#]+(/[^\s?#]*)?$")
CONFIG_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SECRET_WORDS = ("key", "apikey", "token", "secret", "password", "credential")
SETTING_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,190}$")
# Shared address space covers cloud metadata endpoints that is_private misses.
INTERNAL_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)
CREDENTIAL_KEYS = ("credential_secret", "credential_scope")
CREDENTIAL_SCOPES = ("team", "instance")
REQUIRED_CONFIG = {"bedrock": ("region",), "claude-subscription": ("credential_secret",),
                   "openai-compatible": ("api_base",)}

PATH_PART = re.compile(r"\[([^\]]*)\]|([^.\[\]]+)")
SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
LINT_OK = "lint: ok"


def is_secret_name(name: str) -> bool:
    return any(marker in name for marker in SECRET_MARKERS) or name.endswith("_PEM")


def lint_note(data: Any) -> str:
    """The template lint of a parsed workflow, as the first line of a version note and any issues after it."""
    if data is None:
        return "lint: not parsed"
    issues = lint_tree(data)
    if not issues:
        return LINT_OK
    return "\n".join([f"lint: {len(issues)} issue(s)", *(f"- {issue.message}" for issue in issues)])


def lint_ok(note: str | None) -> bool | None:
    """Whether a version note records a clean lint, or None when it records no lint."""
    for line in (note or "").splitlines():
        if line.startswith("lint:"):
            return line == LINT_OK
    return None


class DefError(Exception):
    """A refused definition request: an HTTP status, a message, and any fields the response carries."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class DefScope:
    kind: str
    team_id: str | None = None
    project_id: str | None = None

    @property
    def scope(self) -> str:
        return self.project_id or scope_of(self.team_id)

    @property
    def target(self) -> rbac.Target:
        return rbac.Target(team_id=self.team_id, project_id=self.project_id)

    def columns(self) -> dict[str, Any]:
        return {"team_id": self.team_id, "project_id": self.project_id, "scope": self.scope}


INSTANCE = DefScope("instance")


def row_scope(row: Any) -> DefScope:
    if row.project_id is not None:
        return DefScope("project", row.team_id, row.project_id)
    if row.team_id is not None:
        return DefScope("team", row.team_id)
    return INSTANCE


def load_scope(conn: Connection, kind: Any, scope_id: Any) -> DefScope:
    if kind == "instance":
        if scope_id not in INSTANCE_IDS:
            raise DefError(400, "An instance definition takes no scope_id.")
        return INSTANCE
    if kind not in SCOPE_KINDS:
        raise DefError(400, f"scope_kind must be one of {', '.join(SCOPE_KINDS)}.")
    if not isinstance(scope_id, str) or not scope_id:
        raise DefError(400, "scope_id is required.")
    try:
        target = rbac.scope_target(conn, kind, scope_id)
    except rbac.NotFound as exc:
        raise DefError(404, str(exc)) from exc
    return DefScope(kind, target.team_id, target.project_id)


def can_read(access: rbac.Access, scope: DefScope) -> bool:
    if access.is_admin or scope.kind == "instance":
        return True
    if scope.kind == "team":
        return rbac.sees_team(access, scope.team_id)
    return rbac.role_for(access, scope.target) is not None


def can_write(access: rbac.Access, scope: DefScope) -> bool:
    if access.is_admin:
        return True
    if scope.kind == "instance":
        return False
    return rbac.at_least(rbac.role_for(access, scope.target), "team_lead" if scope.kind == "team" else "project_lead")


def writes_anywhere(access: rbac.Access) -> bool:
    return access.is_admin or any(rbac.at_least(role, "project_lead")
                                  for role in (*access.teams.values(), *access.projects.values()))


def authorize_write(access: rbac.Access, scope: DefScope) -> None:
    """404 for a scope the user can't see, so ids in other teams stay hidden, else 403."""
    if can_write(access, scope):
        return
    if can_read(access, scope):
        raise DefError(403, "Not allowed.")
    raise DefError(404, "Not found.")


def _yaml() -> YAML:
    loader = YAML()
    loader.preserve_quotes = True
    return loader


def plain(value: Any) -> Any:
    """A ruamel tree as plain dicts, lists, and scalars."""
    if isinstance(value, dict):
        return {str(k) if not isinstance(k, (str, int, float, bool)) and k is not None else k: plain(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)


def path_parts(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for index, key in PATH_PART.findall(path):
        if key:
            parts.append(key)
        elif index.isdigit():
            parts.append(int(index))
        else:
            parts.append(index)
    return parts


def join_path(parts: Any) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def position(data: Any, parts: Any) -> tuple[int | None, int | None]:
    """The 1-based line and column of the deepest part of ``parts`` found in a ruamel tree."""
    line = col = None
    node = data
    for part in parts:
        if isinstance(node, CommentedMap) and part in node:
            line, col = node.lc.key(part)
            node = node[part]
        elif isinstance(node, CommentedSeq) and isinstance(part, int) and 0 <= part < len(node):
            line, col = node.lc.item(part)
            node = node[part]
        elif isinstance(node, CommentedSeq) and isinstance(part, str):
            index = next((i for i, item in enumerate(node) if isinstance(item, dict) and item.get("id") == part),
                         None)
            if index is None:
                break
            line, col = node.lc.item(index)
            node = node[index]
        else:
            break
    return (line + 1, col + 1) if line is not None else (None, None)


@dataclass
class Validation:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, severity: str, code: str, message: str, *, path: str = "", line: int | None = None,
            col: int | None = None, offset: int = 0) -> None:
        entry = {"line": line + offset if line is not None else None, "col": col, "code": code,
                 "message": message, "path": path}
        (self.warnings if severity == "warning" else self.errors).append(entry)

    def at(self, data: Any, severity: str, code: str, message: str, parts: Any, offset: int = 0) -> None:
        line, col = position(data, parts)
        self.add(severity, code, message, path=join_path(parts), line=line, col=col, offset=offset)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


@dataclass
class Scan:
    """What the YAML events show before anything is built: a refusal, and where each value scalar sits."""

    problem: tuple[int, int, str] | None = None
    value_spans: list[tuple[int, int]] = field(default_factory=list)


# Anchors and aliases are refused outright, because an alias bomb expands only when the tree is walked.
def scan_yaml(text: str) -> Scan:
    scan = Scan()
    stack: list[list[bool]] = []
    for count, event in enumerate(_yaml().parse(text), 1):
        mark = event.start_mark
        if count > NODE_MAX:
            scan.problem = (mark.line + 1, mark.column + 1, f"A definition has at most {NODE_MAX} YAML nodes.")
            return scan
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
            scan.problem = (mark.line + 1, mark.column + 1, "YAML anchors and aliases are not allowed.")
            return scan
        if isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            stack.pop()
            continue
        if not isinstance(event, (ScalarEvent, MappingStartEvent, SequenceStartEvent)):
            continue
        is_key = bool(stack) and stack[-1][0] and stack[-1][1]
        if stack and stack[-1][0]:
            stack[-1][1] = not stack[-1][1]
        if isinstance(event, ScalarEvent):
            if not is_key:
                scan.value_spans.append((mark.index, event.end_mark.index))
        else:
            stack.append([isinstance(event, MappingStartEvent), True])
    return scan


def _parse(text: str, validation: Validation, offset: int = 0) -> tuple[bool, Any]:
    try:
        scan = scan_yaml(text)
        if scan.problem is not None:
            line, col, message = scan.problem
            validation.add("error", "YAML", message, line=line, col=col, offset=offset)
            return False, None
        return True, _yaml().load(text)
    except YAMLError as exc:
        mark = exc.problem_mark if isinstance(exc, MarkedYAMLError) else None
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        validation.add("error", "YAML", f"YAML did not parse: {problem}",
                       line=mark.line + 1 if mark else None, col=mark.column + 1 if mark else None, offset=offset)
        return False, None


def _quiet_load(text: str | None) -> Any:
    if text is None:
        return None
    parsed, data = _parse(text, Validation())
    return data if parsed else None


def _too_big(body: str, validation: Validation) -> bool:
    if len(body.encode("utf-8")) > BODY_MAX:
        validation.add("error", "TOO_LARGE", f"A definition is at most {BODY_MAX // 1024} KB.")
        return True
    return False


def load_catalog(packs_dir: str | None = None) -> Any:
    return packs.catalog(packs_dir)


def _catalog(catalog: Any) -> Any:
    return load_catalog() if catalog is None else catalog


def _is_admin(actor: Any) -> bool:
    return bool(getattr(actor, "is_admin", False))


def _stage_index(data: Any, stage_id: str | None) -> int | None:
    stages = data.get("stages") if isinstance(data, dict) else None
    if stage_id is None or not isinstance(stages, list):
        return None
    return next((i for i, s in enumerate(stages) if isinstance(s, dict) and s.get("id") == stage_id), None)


def _frozen(validation: Validation, data: Any, parts: list, what: str) -> None:
    validation.at(data, "error", "FROZEN_FIELD", f"Only an admin can change {what}.", parts)


def _same(before: Any, after: Any) -> bool:
    return plain(before) == plain(after)


def _stage_map(stages: Any) -> dict[Any, Any]:
    return {s.get("id"): s for s in stages if isinstance(s, dict)} if isinstance(stages, list) else {}


def _check_adapter(validation: Validation, data: Any, previous: dict, parts: list, value: Any) -> None:
    if isinstance(value, str) and "/" in value:
        return
    before = previous.get("adapters") if isinstance(previous.get("adapters"), dict) else {}
    after = data.get("adapters") if isinstance(data.get("adapters"), dict) else {}
    if not (isinstance(value, str) and value in before and value in after and _same(before[value], after[value])):
        validation.at(data, "error", "FROZEN_FIELD", "A step may use a pack adapter (pack/adapter) or an adapter "
                      "the saved version already defines.", parts)


def _check_stage(validation: Validation, data: Any, previous: dict, index: int, stage: dict,
                 before: dict) -> None:
    for key in sorted(set(stage) | set(before), key=str):
        parts = ["stages", index, key]
        if key in STAGE_MUTABLE:
            if key in stage and (key not in before or not _same(before[key], stage[key])):
                if key == "adapter":
                    _check_adapter(validation, data, previous, parts, stage[key])
                elif key == "timeout_s" and isinstance(stage[key], (int, float)) and stage[key] > TIMEOUT_MAX:
                    validation.at(data, "error", "FROZEN_FIELD", f"timeout_s is at most {TIMEOUT_MAX}.", parts)
            continue
        if key == "role" and isinstance(stage.get(key), dict) and isinstance(before.get(key, {}), dict):
            role, old = stage[key], before.get(key, {})
            for field_name in sorted(set(role) | set(old), key=str):
                if field_name not in ROLE_MUTABLE and not _same(old.get(field_name, ...), role.get(field_name, ...)):
                    _frozen(validation, data, [*parts, field_name], f"stages.{stage.get('id')}.role.{field_name}")
            continue
        if not _same(before.get(key, ...), stage.get(key, ...)):
            _frozen(validation, data, parts, f"stages.{stage.get('id')}.{key}")


def _frozen_rule(validation: Validation, data: Any, previous: Any) -> None:
    """For a non-admin, everything outside the allowlist must match the saved version, or be absent on create."""
    previous = previous if isinstance(previous, dict) else {}
    for key in sorted(set(data) | set(previous), key=str):
        if key in TOP_MUTABLE or key == "stages":
            continue
        if not _same(previous.get(key, ...), data.get(key, ...)):
            _frozen(validation, data, [key], str(key))
    stages, old_stages = data.get("stages"), previous.get("stages")
    if not isinstance(stages, list):
        if not _same(old_stages, stages):
            _frozen(validation, data, ["stages"], "stages")
        return
    old_by_id = _stage_map(old_stages)
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            _frozen(validation, data, ["stages", index], f"stages[{index}]")
            continue
        _check_stage(validation, data, previous, index, stage, old_by_id.get(stage.get("id"), {}))
    kept = set(_stage_map(stages))
    for stage_id, stage in old_by_id.items():
        if stage_id not in kept and any(key not in STAGE_MUTABLE for key in stage):
            validation.add("error", "FROZEN_FIELD", f"Only an admin can remove stage {stage_id}, because it has "
                           "fields other than pack adapter wiring.", path=f"stages[{stage_id}]")


def _templated_commands(validation: Validation, data: Any) -> None:
    adapters = data.get("adapters")
    for name, adapter in (adapters.items() if isinstance(adapters, dict) else ()):
        cmd = adapter.get("cmd") if isinstance(adapter, dict) else None
        if isinstance(cmd, str) and ("{{" in cmd or "{%" in cmd):
            validation.at(data, "error", "TEMPLATED_COMMAND", f"Only an admin can save a workflow whose adapter "
                          f"{name} renders its command from a template.", ["adapters", name, "cmd"])


def _markers_outside_values(validation: Validation, body: str) -> None:
    """The loader renders the raw text, so a marker in a comment or a key is live too."""
    spans = scan_yaml(body).value_spans
    for match in TEMPLATE_MARKER.finditer(body):
        at = match.start()
        if not any(start <= at < end for start, end in spans):
            line = body.count("\n", 0, at) + 1
            col = at - (body.rfind("\n", 0, at) + 1) + 1
            validation.add("error", "UNSAFE_TEMPLATE", f"Template marker {match.group()} outside a value, in a "
                           "comment or a key.", line=line, col=col)


def _const_outputs(text: str) -> bool:
    try:
        tree = text_env().parse(text)
    except TemplateSyntaxError:
        return False
    return any(not isinstance(child, (nodes.TemplateData, nodes.Name)) and next(child.find_all(nodes.Name), None) is None
               for output in tree.find_all(nodes.Output) for child in output.nodes)


def _strings(value: Any, parts: list) -> list[tuple[list, str]]:
    if isinstance(value, str):
        return [(parts, value)]
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in _strings(child, [*parts, key])]
    if isinstance(value, list):
        return [item for i, child in enumerate(value) for item in _strings(child, [*parts, i])]
    return []


def _constant_templates(validation: Validation, data: Any) -> None:
    for parts, text in _strings(data, []):
        if TEMPLATE_MARKER.search(text) and _const_outputs(text):
            validation.at(data, "error", "UNSAFE_TEMPLATE", f"{join_path(parts)}: each {{{{ }}}} must refer to an "
                          "input or a step output, never only a constant.", parts)


def _stored_adapter_fields(validation: Validation, data: Any) -> None:
    adapters = data.get("adapters")
    if not isinstance(adapters, dict):
        return
    for name, adapter in adapters.items():
        for key in RUNTIME_ADAPTER_FIELDS:
            if isinstance(adapter, dict) and key in adapter:
                validation.at(data, "error", "RUNTIME_FIELD",
                              f"adapters.{name}.{key} is set only from an installed pack, never in a workflow.",
                              ["adapters", name, key])


def _lint(validation: Validation, data: Any, previous: Any) -> None:
    existing = {issue.message for issue in lint_tree(previous)} if previous is not None else set()
    for issue in lint_tree(data):
        path = issue.message.split(": ", 1)[0]
        old = issue.message in existing
        validation.at(data, "warning" if old else "error", issue.code,
                      issue.message + (" (already in the saved version)" if old else ""), path_parts(path))


def _pack_refs(validation: Validation, data: Any, catalog: Any) -> None:
    stages = data.get("stages")
    if not isinstance(stages, list):
        return
    refs = [(i, s) for i, s in enumerate(stages)
            if isinstance(s, dict) and isinstance(s.get("adapter"), str) and "/" in s["adapter"]]
    if not refs:
        return
    catalog = _catalog(catalog)
    for i, stage in refs:
        adapter = catalog.get(stage["adapter"])
        if adapter is None:
            validation.at(data, "error", "UNKNOWN_ADAPTER", f"No installed pack has adapter '{stage['adapter']}'.",
                          ["stages", i, "adapter"])
            continue
        given = stage.get("with")
        if given is not None and not isinstance(given, dict):
            validation.at(data, "error", "INVALID_PARAMS", "with must be a mapping.", ["stages", i, "with"])
        else:
            for where, message in with_error_paths(adapter.params, plain(given or {}), allow_templates=True):
                validation.at(data, "error", "INVALID_PARAMS", message, ["stages", i, "with", *where])


def validate_workflow(body: str, *, actor: Any, previous_body: str | None, catalog: Any = None) -> Validation:
    """Parse, schema-check, validate, lint, check pack references, and, for a non-admin, the field allowlist."""
    validation = Validation()
    if _too_big(body, validation):
        return validation
    parsed, data = _parse(body, validation)
    if not parsed:
        return validation
    if not isinstance(data, dict):
        validation.add("error", "NOT_A_MAPPING", "A workflow is a YAML mapping with name and stages.", line=1, col=1)
        return validation
    try:
        spec = HarnessSpec.model_validate(data)
    except ValidationError as exc:
        spec = None
        for err in exc.errors():
            validation.at(data, "error", "SCHEMA", f"{join_path(err['loc']) or 'workflow'}: {err['msg']}",
                          list(err["loc"]))
    if spec is not None:
        for issue in validate_spec(spec, strict=False):
            if issue.code == "UNSAFE_TEMPLATE":
                continue
            index = _stage_index(data, issue.stage_id)
            if issue.code in PACK_REF_CODES and index is not None \
                    and "/" in str(data["stages"][index].get("adapter", "")):
                continue
            validation.at(data, issue.severity, issue.code, issue.message,
                          ["stages", index] if index is not None else [])
    previous = _quiet_load(previous_body)
    _markers_outside_values(validation, body)
    _constant_templates(validation, data)
    _stored_adapter_fields(validation, data)
    _lint(validation, data, previous)
    _pack_refs(validation, data, catalog)
    if not _is_admin(actor):
        _templated_commands(validation, data)
        _frozen_rule(validation, data, previous)
    return validation


FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*$", re.DOTALL | re.MULTILINE)
FRONT_OPEN = re.compile(r"\A---[ \t]*\r?\n")


def front_matter(body: str) -> tuple[str | None, bool]:
    """The front matter text, and whether an opening fence has no closing one."""
    match = FRONT_MATTER.match(body)
    if match:
        return match.group(1), False
    return None, bool(FRONT_OPEN.match(body))


def _items(value: Any) -> set[str]:
    return {item if isinstance(item, str) else json.dumps(plain(item), sort_keys=True) for item in value or []}


def _allowlist(catalog: Any, name: str) -> set[str]:
    method = getattr(catalog, name, None)
    return set(method()) if callable(method) else set()


def validate_document(kind: str, body: str, *, actor: Any, previous_body: str | None,
                      catalog: Any = None) -> Validation:
    validation = Validation()
    if kind not in DOCUMENT_KINDS:
        validation.add("error", "KIND", f"kind must be one of {', '.join(DOCUMENT_KINDS)}.")
        return validation
    if _too_big(body, validation):
        return validation
    text, unclosed = front_matter(body)
    if unclosed:
        validation.add("error", "FRONT_MATTER", "Front matter opens with --- and never closes.", line=1, col=1)
        return validation
    data: Any = None
    if text is not None:
        parsed, data = _parse(text, validation, offset=1)
        if not parsed:
            return validation
        if data is not None and not isinstance(data, dict):
            validation.add("error", "FRONT_MATTER", "Front matter must be a YAML mapping.", line=2, col=1)
            return validation
    if kind == "role":
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name.strip():
            validation.add("error", "ROLE_NAME", "A role needs a name in its front matter.", line=1, col=1)
    if not isinstance(data, dict):
        return validation
    for key in GUARDED_FRONT_MATTER:
        if key in data and not isinstance(data[key], list):
            validation.at(data, "error", "FRONT_MATTER", f"{key} must be a list.", [key], offset=1)
    if _is_admin(actor):
        return validation
    previous_text = front_matter(previous_body)[0] if previous_body is not None else None
    previous = _quiet_load(previous_text) if previous_text is not None else None
    previous = previous if isinstance(previous, dict) else {}
    loaded: list[Any] = []
    for key, allow_name in GUARDED_FRONT_MATTER.items():
        if not isinstance(data.get(key), list):
            continue
        before = previous.get(key) if isinstance(previous.get(key), list) else []
        added = _items(data[key]) - _items(before)
        if not added:
            continue
        if not loaded:
            loaded.append(_catalog(catalog))
        allowed = _allowlist(loaded[0], allow_name)
        wider = sorted(added - allowed)
        if wider:
            validation.at(data, "error", "NOT_ALLOWLISTED",
                          f"Only an admin can add {key} outside the installed packs' allowlist: {', '.join(wider)}.",
                          [key], offset=1)
    return validation


def _detect_indent(body: str) -> tuple[int, int]:
    match = re.search(r"^stages:[ \t]*\r?\n((?:[ \t]*#.*\r?\n|[ \t]*\r?\n)*)( *)- ", body, re.MULTILINE)
    offset = len(match.group(2)) if match else 0
    return offset + 2, offset


def parse_stages(body: str) -> dict[str, Any]:
    validation = Validation()
    parsed, data = _parse(body, validation)
    if parsed and not isinstance(data, dict):
        validation.add("error", "NOT_A_MAPPING", "A workflow is a YAML mapping with name and stages.", line=1, col=1)
    if not validation.ok:
        return {"stages": [], "adapters": [], "errors": validation.errors}
    stages = data.get("stages") if isinstance(data.get("stages"), list) else []
    listed = []
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        item = {"id": plain(stage.get("id"))}
        for name, empty in STAGE_FIELDS.items():
            item[name] = plain(stage.get(name, empty))
        item["line"] = stages.lc.item(i)[0] + 1
        listed.append(item)
    adapters = data.get("adapters") if isinstance(data.get("adapters"), dict) else {}
    return {"stages": listed, "adapters": [str(name) for name in adapters], "errors": []}


def _flow(value: list[Any]) -> CommentedSeq:
    seq = CommentedSeq(value)
    seq.fa.set_flow_style()
    return seq


def render_stages(body: str, stages: Any) -> str:
    """Write the form editor's stages back into ``body``, keeping comments, order, and every other key."""
    if not isinstance(stages, list) or not all(isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"]
                                               for s in stages):
        raise DefError(400, "stages must be a list of objects, each with an id.")
    if any(s.get("prev_id") is not None and not isinstance(s.get("prev_id"), str) for s in stages):
        raise DefError(400, "prev_id must be a string.")
    for stage in stages:
        if not isinstance(stage.get("with", {}) or {}, dict):
            raise DefError(400, f"Stage {stage['id']}: with must be an object.")
        if not isinstance(stage.get("depends_on", []) or [], list):
            raise DefError(400, f"Stage {stage['id']}: depends_on must be a list.")
    validation = Validation()
    parsed, data = _parse(body, validation)
    if not parsed or not isinstance(data, dict):
        raise DefError(422, "invalid", validation=validation.to_dict() if not validation.ok else
                       {"ok": False, "errors": [{"line": 1, "col": 1, "code": "NOT_A_MAPPING",
                                                 "message": "A workflow is a YAML mapping.", "path": ""}],
                        "warnings": []})
    existing = data.get("stages") if isinstance(data.get("stages"), CommentedSeq) else CommentedSeq()
    by_id = {s.get("id"): s for s in existing if isinstance(s, CommentedMap)}
    rebuilt = []
    for stage in stages:
        prev_id = stage.get("prev_id") or stage["id"]
        target = by_id.get(prev_id)
        if target is None:
            target = CommentedMap()
            target["id"] = stage["id"]
        elif target.get("id") != stage["id"]:
            target["id"] = stage["id"]
        for name, empty in STAGE_FIELDS.items():
            new = stage.get(name, empty)
            if new is None or (new == "" and empty is None):
                new = empty
            if name in target and plain(target[name]) == new:
                continue
            if name not in target and new == empty:
                continue
            if new == empty:
                target.pop(name, None)
            elif name == "depends_on":
                target[name] = _flow(list(new))
            else:
                target[name] = new
        rebuilt.append(target)
    if isinstance(data.get("stages"), CommentedSeq):
        seq = data["stages"]
        unchanged = [id(s) for s in seq] == [id(s) for s in rebuilt]
        if not unchanged:
            del seq[:]
            seq.extend(rebuilt)
    else:
        data["stages"] = CommentedSeq(rebuilt)
    out = io.StringIO()
    dumper = _yaml()
    sequence, offset = _detect_indent(body)
    dumper.indent(mapping=2, sequence=sequence, offset=offset)
    dumper.width = 4096
    dumper.dump(data, out)
    return out.getvalue()


@dataclass(frozen=True)
class Kind:
    object_kind: str
    table: Table
    name_col: str


WORKFLOW = Kind("workflow", workflows, "name")
DOCUMENT = Kind("document", documents, "key")


def _validate(kind: Kind, body: str, *, access: rbac.Access, previous_body: str | None, doc_kind: str | None,
              catalog: Any) -> Validation:
    if kind is WORKFLOW:
        return validate_workflow(body, actor=access, previous_body=previous_body, catalog=catalog)
    return validate_document(doc_kind or "", body, actor=access, previous_body=previous_body, catalog=catalog)


def get_row(conn: Connection, kind: Kind, object_id: str) -> Any | None:
    return conn.execute(select(kind.table).where(kind.table.c.id == object_id)).first()


def readable_row(conn: Connection, kind: Kind, object_id: str, access: rbac.Access) -> Any:
    row = get_row(conn, kind, object_id)
    if row is None or not can_read(access, row_scope(row)):
        raise DefError(404, "Not found.")
    return row


def body_of(conn: Connection, kind: Kind, object_id: str, version: int) -> str | None:
    return conn.execute(select(versions.c.body).where(
        versions.c.object_kind == kind.object_kind, versions.c.object_id == object_id,
        versions.c.version == version)).scalar()


def version_detail(conn: Connection, kind: Kind, object_id: str, version: int) -> dict[str, Any] | None:
    row = conn.execute(
        select(versions.c.version, versions.c.body, versions.c.note, versions.c.created_at,
               users.c.username, versions.c.author_id)
        .select_from(versions.outerjoin(users, users.c.id == versions.c.author_id))
        .where(versions.c.object_kind == kind.object_kind, versions.c.object_id == object_id,
               versions.c.version == version)).first()
    if row is None:
        return None
    return {"version": row.version, "body": row.body, "note": row.note, "author": row.username or row.author_id,
            "created_at": row.created_at}


def diff(conn: Connection, kind: Kind, row: Any, start: int | None, end: int | None) -> str:
    end = row.current_version if end is None else end
    start = end - 1 if start is None else start
    before = body_of(conn, kind, row.id, start) if start > 0 else ""
    after = body_of(conn, kind, row.id, end)
    if before is None or after is None:
        raise DefError(404, "No such version.")
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"version {start}", tofile=f"version {end}"))


def _note(kind: Kind, body: str, note: Any) -> str | None:
    if note is not None and not isinstance(note, str):
        raise DefError(400, "note must be a string.")
    # The list reads lint status from a "lint:" line, which only the server writes.
    text = "\n".join(line for line in (note or "").strip()[:NOTE_MAX].splitlines() if not line.startswith("lint:"))
    if kind is not WORKFLOW:
        return text or None
    lint = lint_note(_quiet_load(body))
    return f"{text}\n{lint}" if text else lint


def _check_body(body: Any) -> str:
    if not isinstance(body, str):
        raise DefError(400, "body must be a string.")
    return body


def _check_name(kind: Kind, name: Any) -> str:
    pattern = WORKFLOW_NAME if kind is WORKFLOW else DOCUMENT_KEY
    if not isinstance(name, str) or not pattern.fullmatch(name):
        what = "A workflow name" if kind is WORKFLOW else "A document key"
        raise DefError(400, f"{what} is 1 to 100 letters, digits, '.', '_', or '-'.")
    return name


def _invalid(validation: Validation) -> DefError:
    return DefError(422, "invalid", validation=validation.to_dict())


def _add_version(conn: Connection, kind: Kind, object_id: str, version: int, body: str, note: str | None,
                 actor_id: str, now: datetime) -> None:
    conn.execute(insert(versions).values(id=new_id(), object_kind=kind.object_kind, object_id=object_id,
                                         version=version, body=body, author_id=actor_id, created_at=now, note=note))


def _audit(conn: Connection, kind: Kind, action: str, row_id: str, scope: DefScope, actor_id: str, ip: str | None,
           **detail: Any) -> None:
    audit.record(conn, action=f"{kind.object_kind}.{action}", actor_id=actor_id, team_id=scope.team_id,
                 target_kind=kind.object_kind, target_id=row_id,
                 detail={"scope": scope.scope, "scope_kind": scope.kind, **detail}, ip=ip)


def _name_taken(conn: Connection, kind: Kind, scope: DefScope, name: str, doc_kind: str | None) -> bool:
    where = [kind.table.c.scope == scope.scope, kind.table.c[kind.name_col] == name]
    if kind is DOCUMENT:
        where.append(documents.c.kind == doc_kind)
    return conn.execute(select(kind.table.c.id).where(*where)).first() is not None


def _insert(conn: Connection, kind: Kind, scope: DefScope, name: str, doc_kind: str | None, body: str,
            note: str | None, actor_id: str, now: datetime) -> str:
    if _name_taken(conn, kind, scope, name, doc_kind):
        raise DefError(409, f"That scope already has a {kind.object_kind} named {name}.")
    row_id = new_id()
    values = {"id": row_id, kind.name_col: name, "current_version": 1, "created_at": now, "updated_at": now,
              **scope.columns()}
    if kind is DOCUMENT:
        values["kind"] = doc_kind
    conn.execute(insert(kind.table).values(**values))
    _add_version(conn, kind, row_id, 1, body, note, actor_id, now)
    return row_id


def create(conn: Connection, kind: Kind, *, access: rbac.Access, scope: DefScope, name: Any, body: Any, note: Any,
           actor_id: str, now: datetime, ip: str | None, doc_kind: str | None = None,
           catalog: Any = None) -> tuple[Any, Validation]:
    authorize_write(access, scope)
    name, body = _check_name(kind, name), _check_body(body)
    if kind is DOCUMENT and doc_kind not in DOCUMENT_KINDS:
        raise DefError(400, f"kind must be one of {', '.join(DOCUMENT_KINDS)}.")
    validation = _validate(kind, body, access=access, previous_body=None, doc_kind=doc_kind, catalog=catalog)
    if not validation.ok:
        raise _invalid(validation)
    row_id = _insert(conn, kind, scope, name, doc_kind, body, _note(kind, body, note), actor_id, now)
    _audit(conn, kind, "create", row_id, scope, actor_id, ip, name=name, version=1)
    return get_row(conn, kind, row_id), validation


def _advance(conn: Connection, kind: Kind, row: Any, base: int, body: str, note: str | None, actor_id: str,
             now: datetime) -> int:
    result = conn.execute(update(kind.table).where(kind.table.c.id == row.id, kind.table.c.current_version == base)
                          .values(current_version=base + 1, updated_at=now))
    if result.rowcount != 1:
        current = conn.execute(select(kind.table.c.current_version).where(kind.table.c.id == row.id)).scalar()
        raise DefError(409, "conflict", current_version=current)
    _add_version(conn, kind, row.id, base + 1, body, note, actor_id, now)
    return base + 1


def save(conn: Connection, kind: Kind, row: Any, *, access: rbac.Access, body: Any, note: Any, base_version: Any,
         actor_id: str, now: datetime, ip: str | None, catalog: Any = None) -> tuple[Any, Validation]:
    scope = row_scope(row)
    authorize_write(access, scope)
    body = _check_body(body)
    if not isinstance(base_version, int) or isinstance(base_version, bool):
        raise DefError(400, "base_version must be an integer.")
    if base_version != row.current_version:
        raise DefError(409, "conflict", current_version=row.current_version)
    previous = body_of(conn, kind, row.id, row.current_version)
    validation = _validate(kind, body, access=access, previous_body=previous,
                           doc_kind=getattr(row, "kind", None), catalog=catalog)
    if not validation.ok:
        raise _invalid(validation)
    version = _advance(conn, kind, row, base_version, body, _note(kind, body, note), actor_id, now)
    _audit(conn, kind, "version", row.id, scope, actor_id, ip, name=getattr(row, kind.name_col), version=version)
    return get_row(conn, kind, row.id), validation


def fork(conn: Connection, kind: Kind, row: Any, *, access: rbac.Access, dest: DefScope, name: Any,
         actor_id: str, now: datetime, ip: str | None, catalog: Any = None) -> Any:
    source = row_scope(row)
    if not can_read(access, source):
        raise DefError(404, "Not found.")
    authorize_write(access, dest)
    name = _check_name(kind, getattr(row, kind.name_col) if name in (None, "") else name)
    body = body_of(conn, kind, row.id, row.current_version)
    if body is None:
        raise DefError(409, f"This {kind.object_kind} has no saved version to fork.")
    doc_kind = getattr(row, "kind", None)
    validation = _validate(kind, body, access=access, previous_body=body, doc_kind=doc_kind, catalog=catalog)
    if not validation.ok:
        raise _invalid(validation)
    note = _note(kind, body, f"Forked from {source.kind} {getattr(row, kind.name_col)} version {row.current_version}.")
    row_id = _insert(conn, kind, dest, name, doc_kind, body, note, actor_id, now)
    _audit(conn, kind, "fork", row_id, dest, actor_id, ip, name=name, source_id=row.id,
           source_version=row.current_version)
    return get_row(conn, kind, row_id)


def rollback(conn: Connection, kind: Kind, row: Any, *, access: rbac.Access, version: Any, note: Any,
             actor_id: str, now: datetime, ip: str | None, catalog: Any = None, base_version: int | None = None) -> Any:
    scope = row_scope(row)
    authorize_write(access, scope)
    if not isinstance(version, int) or isinstance(version, bool):
        raise DefError(400, "version must be an integer.")
    if base_version is not None and base_version != row.current_version:
        raise DefError(409, "conflict", current_version=row.current_version)
    body = body_of(conn, kind, row.id, version)
    if body is None:
        raise DefError(404, "No such version.")
    previous = body_of(conn, kind, row.id, row.current_version)
    validation = _validate(kind, body, access=access, previous_body=previous, doc_kind=getattr(row, "kind", None),
                           catalog=catalog)
    if not validation.ok:
        raise _invalid(validation)
    text = note if isinstance(note, str) and note.strip() else f"Rolled back to version {version}."
    new = _advance(conn, kind, row, row.current_version, body, _note(kind, body, text), actor_id, now)
    _audit(conn, kind, "rollback", row.id, scope, actor_id, ip, name=getattr(row, kind.name_col), version=new,
           from_version=version)
    return get_row(conn, kind, row.id)


def remove(conn: Connection, kind: Kind, row: Any, *, access: rbac.Access, actor_id: str, ip: str | None) -> None:
    """Delete the definition. Its versions stay, as history, and past runs keep their workflow name."""
    scope = row_scope(row)
    authorize_write(access, scope)
    if kind is WORKFLOW:
        if conn.execute(select(jobs.c.id).where(jobs.c.workflow_id == row.id, jobs.c.status.in_(ACTIVE_JOBS))
                        .limit(1)).first() is not None:
            raise DefError(409, "This workflow has queued or running jobs.")
        conn.execute(delete(jobs).where(jobs.c.workflow_id == row.id))
        conn.execute(delete(triggers).where(triggers.c.workflow_id == row.id))
        conn.execute(update(runs).where(runs.c.workflow_id == row.id).values(workflow_id=None))
    conn.execute(delete(kind.table).where(kind.table.c.id == row.id))
    _audit(conn, kind, "delete", row.id, scope, actor_id, ip, name=getattr(row, kind.name_col),
           version=row.current_version)


def _resolve(conn: Connection, table: Table, where: list, team_id: str | None, project_id: str | None) -> Any | None:
    order = [s for s in (project_id, team_id, INSTANCE_SCOPE) if s is not None]
    rows = {r.scope: r for r in conn.execute(select(table).where(*where, table.c.scope.in_(order)))}
    return next((rows[s] for s in order if s in rows), None)


def resolve_workflow(conn: Connection, name: str, team_id: str | None, project_id: str | None) -> Any | None:
    """The workflow a run of ``name`` uses: the project's, else the team's, else the instance's."""
    return _resolve(conn, workflows, [workflows.c.name == name], team_id, project_id)


def resolve_document(conn: Connection, kind: str, key: str, team_id: str | None,
                     project_id: str | None) -> Any | None:
    return _resolve(conn, documents, [documents.c.kind == kind, documents.c.key == key], team_id, project_id)


@dataclass(frozen=True)
class Context:
    team_id: str
    project_id: str | None


def _contexts(conn: Connection, access: rbac.Access, team: str | None, project: str | None) -> list[Context]:
    rows = conn.execute(select(projects.c.id, projects.c.team_id)).all()
    team_ids = [r.id for r in conn.execute(select(teams.c.id))]
    contexts = [Context(t, None) for t in team_ids if can_read(access, DefScope("team", t))]
    contexts += [Context(r.team_id, r.id) for r in rows if can_read(access, DefScope("project", r.team_id, r.id))]
    if project is not None:
        return [c for c in contexts if c.project_id == project]
    if team is not None:
        return [c for c in contexts if c.team_id == team]
    return contexts


def scope_names(conn: Connection) -> dict[str, str]:
    names = {r.id: r.name for r in conn.execute(select(teams.c.id, teams.c.name))}
    names.update({r.id: r.name for r in conn.execute(select(projects.c.id, projects.c.name))})
    return names


def _filter_scope(conn: Connection, access: rbac.Access, team: str | None, project: str | None) -> DefScope | None:
    if project is not None:
        scope = load_scope(conn, "project", project)
    elif team is not None:
        scope = load_scope(conn, "team", team)
    else:
        return None
    if not can_read(access, scope):
        raise DefError(404, "Not found.")
    return scope


def _applies(scope: DefScope, focus: DefScope | None) -> bool:
    if focus is None or scope.kind == "instance":
        return True
    if focus.kind == "team":
        return scope.team_id == focus.team_id
    return scope.project_id == focus.project_id or (scope.kind == "team" and scope.team_id == focus.team_id)


def summary(kind: Kind, row: Any, access: rbac.Access, names: dict[str, str]) -> dict[str, Any]:
    scope = row_scope(row)
    scope_id = scope.project_id or scope.team_id or INSTANCE_SCOPE
    item = {"id": row.id, kind.name_col: getattr(row, kind.name_col), "scope_kind": scope.kind, "scope_id": scope_id,
            "scope_name": names.get(scope_id, "Instance") if scope.kind != "instance" else "Instance",
            "scope": row.scope, "team_id": row.team_id, "project_id": row.project_id,
            "current_version": row.current_version, "updated_at": row.updated_at,
            "editable": can_write(access, scope), "can_fork": writes_anywhere(access)}
    if kind is DOCUMENT:
        item["kind"] = row.kind
    return item


def listing(conn: Connection, kind: Kind, access: rbac.Access, *, team: str | None = None,
            project: str | None = None, doc_kind: str | None = None, extra: Any = None) -> list[dict[str, Any]]:
    """Visible definitions, each with the teams and projects whose runs resolve its name to it."""
    focus = _filter_scope(conn, access, team, project)
    query = select(kind.table, *(extra or ()))
    if extra is not None:
        query = query.select_from(kind.table.outerjoin(versions, (versions.c.object_kind == kind.object_kind)
                                                       & (versions.c.object_id == kind.table.c.id)
                                                       & (versions.c.version == kind.table.c.current_version)))
    if doc_kind is not None:
        query = query.where(documents.c.kind == doc_kind)
    rows = [r for r in conn.execute(query.order_by(kind.table.c[kind.name_col], kind.table.c.scope))
            if can_read(access, row_scope(r)) and _applies(row_scope(r), focus)]
    names = scope_names(conn)
    grouped: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (getattr(r, "kind", None), getattr(r, kind.name_col))
        grouped.setdefault(key, {})[r.scope] = r.id
    effective: dict[str, list[dict[str, str]]] = {r.id: [] for r in rows}
    for context in _contexts(conn, access, focus.team_id if focus else None, focus.project_id if focus else None):
        for by_scope in grouped.values():
            winner = next((by_scope[s] for s in (context.project_id, context.team_id, INSTANCE_SCOPE)
                           if s is not None and s in by_scope), None)
            if winner is not None:
                scope_id = context.project_id or context.team_id
                effective[winner].append({"scope_kind": "project" if context.project_id else "team",
                                          "scope_id": scope_id, "scope_name": names.get(scope_id)})
    out = []
    for r in rows:
        item = summary(kind, r, access, names)
        if kind is WORKFLOW:
            item["lint_ok"] = lint_ok(getattr(r, "note", None))
        item["effective_for"] = effective[r.id]
        out.append(item)
    return out


def provider_target(conn: Connection, provider_id: str) -> rbac.Target:
    team_id = conn.execute(select(providers.c.team_id).where(providers.c.id == provider_id)).scalar()
    if team_id is None:
        raise rbac.NotFound("No such provider.")
    return rbac.Target(team_id=team_id)


def resolve_host(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def _internal(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or any(ip in network for network in INTERNAL_NETWORKS))


# A team's provider calls go out from this host, so a non-admin can't point one at the host's own network.
def _check_public_api_base(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise DefError(400, "api_base must be an https URL. Ask an admin for anything else.")
    try:
        addresses = [str(ipaddress.ip_address(parts.hostname))]
    except ValueError:
        try:
            addresses = resolve_host(parts.hostname)
        except OSError as exc:
            raise DefError(400, f"api_base host {parts.hostname} does not resolve.") from exc
    if not addresses or any(_internal(address) for address in addresses):
        raise DefError(400, "api_base points at a private, loopback, link-local, or metadata address. "
                            "Ask an admin to save it.")


def check_provider(kind: Any, config: Any, aliases: Any, credential_secret: Any, *, is_admin: bool = False,
                   previous: dict | None = None) -> tuple[dict, dict]:
    if kind not in PROVIDER_KINDS:
        raise DefError(400, f"kind must be one of {', '.join(PROVIDER_KINDS)}.")
    config = {} if config is None else config
    aliases = {} if aliases is None else aliases
    if not isinstance(config, dict) or not isinstance(aliases, dict):
        raise DefError(400, "config and aliases must be objects.")
    config = dict(config)
    if credential_secret not in (None, ""):
        config["credential_secret"] = credential_secret
    for key, value in config.items():
        if not CONFIG_KEY.fullmatch(str(key)):
            raise DefError(400, f"Config key {key!r} is not valid.")
        if key not in CREDENTIAL_KEYS and any(word in SECRET_WORDS for word in key.split("_")):
            raise DefError(400, f"Config key {key} looks like a credential. Store it as a secret and name it in "
                                "credential_secret.")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise DefError(400, f"Config value {key} must be a string, number, or boolean.")
        if isinstance(value, str) and len(value) > 512:
            raise DefError(400, f"Config value {key} is too long.")
    secret = config.get("credential_secret")
    if secret is not None and (not isinstance(secret, str) or not SECRET_NAME.fullmatch(secret)):
        raise DefError(400, "credential_secret is the name of a secret: letters, digits, and '_'.")
    if config.get("credential_scope", "team") not in CREDENTIAL_SCOPES:
        raise DefError(400, f"credential_scope must be one of {', '.join(CREDENTIAL_SCOPES)}.")
    if config.get("credential_scope") == "instance" and not is_admin \
            and (previous or {}).get("credential_scope") != "instance":
        raise DefError(403, "Only an admin can point a provider at an instance secret.")
    for key in REQUIRED_CONFIG.get(kind, ()):
        if config.get(key) in (None, ""):
            raise DefError(400, f"A {kind} provider needs {key}.")
    if config.get("region") is not None and not (isinstance(config["region"], str)
                                                 and REGION.fullmatch(config["region"])):
        raise DefError(400, "region is an AWS region such as us-east-1.")
    if config.get("api_base") is not None:
        if not (isinstance(config["api_base"], str) and API_BASE.fullmatch(config["api_base"])):
            raise DefError(400, "api_base is an http or https URL.")
        if not is_admin and config["api_base"] != (previous or {}).get("api_base"):
            _check_public_api_base(config["api_base"])
    for alias, model in aliases.items():
        if not ALIAS.fullmatch(str(alias)):
            raise DefError(400, f"Alias {alias!r} must match {ALIAS.pattern}.")
        if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
            raise DefError(400, f"Alias {alias} needs a model id.")
    return config, aliases


def credential_team(row: Any) -> str | None:
    """The team whose secrets hold the provider's credential, or None for the instance. Never both."""
    return None if (row.config or {}).get("credential_scope") == "instance" else row.team_id


def _credential_row(conn: Connection, row: Any) -> Any | None:
    name = (row.config or {}).get("credential_secret")
    if not name:
        return None
    return conn.execute(select(secrets.c.id, secrets.c.ciphertext).where(
        secrets.c.scope == scope_of(credential_team(row)), secrets.c.name == name)).first()


def provider_configured(conn: Connection, row: Any) -> bool:
    return not (row.config or {}).get("credential_secret") or _credential_row(conn, row) is not None


def provider_credential(conn: Connection, keyring: Any, row: Any) -> str | None:
    found = _credential_row(conn, row)
    if found is None:
        return None
    name = row.config["credential_secret"]
    return keyring.open_(found.ciphertext, secret_store.aad(credential_team(row), found.id, name))


def provider_public(row: Any) -> dict[str, Any]:
    return {"id": row.id, "team_id": row.team_id, "kind": row.kind, "config": row.config or {},
            "aliases": row.aliases or {}, "created_at": row.created_at, "updated_at": row.updated_at}


def create_provider(conn: Connection, *, team_id: str, body: dict[str, Any], actor_id: str, now: datetime,
                    ip: str | None, is_admin: bool = False) -> Any:
    config, aliases = check_provider(body.get("kind"), body.get("config"), body.get("aliases"),
                                     body.get("credential_secret"), is_admin=is_admin)
    if conn.execute(select(providers.c.id).where(providers.c.team_id == team_id,
                                                 providers.c.kind == body["kind"])).first() is not None:
        raise DefError(409, f"This team already has a {body['kind']} provider.")
    provider_id = new_id()
    conn.execute(insert(providers).values(id=provider_id, team_id=team_id, kind=body["kind"], config=config,
                                          aliases=aliases, created_at=now, updated_at=now))
    audit.record(conn, action="provider.create", actor_id=actor_id, team_id=team_id, target_kind="provider",
                 target_id=provider_id, detail={"kind": body["kind"], "aliases": sorted(aliases)}, ip=ip)
    return conn.execute(select(providers).where(providers.c.id == provider_id)).one()


def update_provider(conn: Connection, *, provider_id: str, body: dict[str, Any], actor_id: str, now: datetime,
                    ip: str | None, is_admin: bool = False) -> Any:
    row = conn.execute(select(providers).where(providers.c.id == provider_id)).first()
    if row is None:
        raise DefError(404, "No such provider.")
    kind = body.get("kind", row.kind)
    if kind != row.kind:
        raise DefError(400, "A provider's kind can't change. Add a provider of the new kind instead.")
    config, aliases = check_provider(kind, body.get("config", row.config), body.get("aliases", row.aliases),
                                     body.get("credential_secret"), is_admin=is_admin, previous=row.config or {})
    if not is_admin and (row.config or {}).get("credential_scope") == "instance" and config != (row.config or {}):
        raise DefError(403, "This provider uses an instance secret, so only an admin can change its credential, "
                            "region, or api_base. You can edit its aliases.")
    conn.execute(update(providers).where(providers.c.id == provider_id)
                 .values(config=config, aliases=aliases, updated_at=now))
    audit.record(conn, action="provider.update", actor_id=actor_id, team_id=row.team_id, target_kind="provider",
                 target_id=provider_id, detail={"kind": kind, "aliases": sorted(aliases)}, ip=ip)
    return conn.execute(select(providers).where(providers.c.id == provider_id)).one()


def delete_provider(conn: Connection, *, provider_id: str, actor_id: str, ip: str | None) -> None:
    row = conn.execute(select(providers).where(providers.c.id == provider_id)).first()
    if row is None:
        raise DefError(404, "No such provider.")
    conn.execute(delete(providers).where(providers.c.id == provider_id))
    audit.record(conn, action="provider.delete", actor_id=actor_id, team_id=row.team_id, target_kind="provider",
                 target_id=provider_id, detail={"kind": row.kind}, ip=ip)


def setting_scope(conn: Connection, scope: Any) -> str | None:
    """The team id a settings scope names, or None for the instance."""
    if scope in INSTANCE_IDS:
        return None
    if not isinstance(scope, str):
        raise DefError(400, "scope is 'instance' or a team id.")
    try:
        return rbac.team_target(conn, scope).team_id
    except rbac.NotFound as exc:
        raise DefError(404, str(exc)) from exc


def check_setting_key(key: Any) -> str:
    if not isinstance(key, str) or not SETTING_KEY.fullmatch(key):
        raise DefError(400, "A setting key is 1 to 191 letters, digits, '.', '_', or '-'.")
    if is_secret_name(key.upper()):
        raise DefError(400, f"{key} looks like a secret. Store it under Secrets instead.")
    return key


def put_setting(conn: Connection, *, team_id: str | None, key: Any, value: Any, actor_id: str, now: datetime,
                ip: str | None) -> dict[str, Any]:
    key = check_setting_key(key)
    if key.startswith(display.PREFIX):
        try:
            value = display.check(key, value, team_id)
        except display.Invalid as exc:
            raise DefError(400, str(exc)) from exc
    scope = scope_of(team_id)
    where = (settings.c.scope == scope, settings.c.key == key)
    exists = conn.execute(select(settings.c.key).where(*where)).first() is not None
    values = {"value": value, "team_id": team_id, "scope": scope, "updated_at": now, "updated_by": actor_id}
    if exists:
        conn.execute(update(settings).where(*where).values(**values))
    else:
        conn.execute(insert(settings).values(key=key, **values))
    audit.record(conn, action="setting.set", actor_id=actor_id, team_id=team_id, target_kind="setting",
                 target_id=key, detail={"key": key, "scope": scope}, ip=ip)
    return {"key": key, "value": value, "scope": scope}


def delete_setting(conn: Connection, *, team_id: str | None, key: Any, actor_id: str, ip: str | None) -> None:
    if not isinstance(key, str):
        raise DefError(400, "key is required.")
    scope = scope_of(team_id)
    result = conn.execute(delete(settings).where(settings.c.scope == scope, settings.c.key == key))
    if result.rowcount != 1:
        raise DefError(404, "No such setting.")
    audit.record(conn, action="setting.delete", actor_id=actor_id, team_id=team_id, target_kind="setting",
                 target_id=key, detail={"key": key, "scope": scope}, ip=ip)
