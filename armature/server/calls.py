"""The stored workflows a workflow's ``call:`` steps run, resolved from the database.

A call resolves only in the caller's own scope: a project's workflow calls
workflows of that project, and a legacy team or instance workflow calls
workflows of that same team or the instance. Nothing falls through to a wider
scope, so one project never runs another's workflow. A call without
``version:`` runs the callee's current version when the job is queued, and the
job keeps that pin.
"""
from __future__ import annotations

import json
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import Connection, select

from armature.server.db.schema import versions, workflows
from armature.spec.calls import (
    MANIFEST,
    CallNotFound,
    CallResolver,
    ResolvedCall,
    bundle_file_name,
    call_graph_refs,
)
from armature.spec.validator import SpecError

MAX_CALLEES = 50
MAX_DEPTH = 10
TOO_LARGE = "CALL_GRAPH_TOO_LARGE"

Ref = tuple[str, str, int | None]


class GraphTooLarge(Exception):
    pass


def refs(data: Any) -> list[Ref]:
    """The (stage id, workflow, version) of each ``call:`` step in a parsed workflow."""
    stages = data.get("stages") if isinstance(data, dict) else None
    found = []
    for stage in stages if isinstance(stages, list) else ():
        call = stage.get("call") if isinstance(stage, dict) else None
        if isinstance(call, dict) and isinstance(call.get("workflow"), str):
            version = call.get("version")
            found.append((str(stage.get("id")), call["workflow"],
                          version if isinstance(version, int) and not isinstance(version, bool) else None))
    return found


def body_refs(body: str | None) -> list[Ref]:
    try:
        return refs(YAML(typ="safe").load(body) if body else None)
    except YAMLError:
        return []


class DbCallResolver:
    """Resolves calls among the workflows of one scope. ``pins`` fixes the version of a call without one."""

    def __init__(self, conn: Connection, scope: str, pins: dict[str, int] | None = None):
        self.conn = conn
        self.scope = scope
        self.pins = dict(pins) if pins is not None else None
        self.resolved: dict[str, int] = {}
        self._cache: dict[tuple[str, int | None], ResolvedCall] = {}

    def resolve(self, workflow: str, version: int | None) -> ResolvedCall:
        key = (workflow, version)
        if key not in self._cache:
            self._cache[key] = self._load(workflow, version)
        found = self._cache[key]
        if version is None:
            self.resolved[workflow] = found.version
        return found

    def _load(self, workflow: str, version: int | None) -> ResolvedCall:
        row = self.conn.execute(select(workflows.c.id, workflows.c.name, workflows.c.current_version).where(
            workflows.c.scope == self.scope, workflows.c.name == workflow)).first()
        # MySQL compares names without case, so the name is matched again here.
        if row is None or row.name != workflow:
            raise CallNotFound(f"no workflow named '{workflow}' in the caller's own project, team, or instance")
        if version is None:
            version = row.current_version if self.pins is None else self.pins.get(workflow)
            if version is None:
                raise CallNotFound(f"workflow '{workflow}' has no pinned version in this job")
        body = self.conn.execute(select(versions.c.body).where(
            versions.c.object_kind == "workflow", versions.c.object_id == row.id,
            versions.c.version == version)).scalar()
        if body is None:
            raise CallNotFound(f"workflow '{workflow}' has no version {version}")
        return ResolvedCall(workflow, version, body)


class _Bounded:
    def __init__(self, inner: CallResolver):
        self.inner = inner
        self.seen: set[tuple[str, int]] = set()

    def resolve(self, workflow: str, version: int | None) -> ResolvedCall:
        found = self.inner.resolve(workflow, version)
        self.seen.add((found.workflow, found.version))
        if len(self.seen) > MAX_CALLEES:
            raise GraphTooLarge(f"A workflow may call at most {MAX_CALLEES} workflows, counting every level.")
        return found


def _depth(calls: list[ResolvedCall], root_refs: list[Ref], resolver: CallResolver) -> int:
    """The longest chain of calls from the root. Cycles are reported elsewhere, so they end a chain here."""
    def key(workflow: str, version: int | None) -> tuple[str, int] | None:
        try:
            found = resolver.resolve(workflow, version)
        except CallNotFound:
            return None
        return found.workflow, found.version

    children = {(c.workflow, c.version): [k for _, w, v in body_refs(c.text) if (k := key(w, v))] for c in calls}
    memo: dict[tuple[str, int], int] = {}

    def longest(node: tuple[str, int], path: frozenset) -> int:
        if node in memo:
            return memo[node]
        depth = 1 + max((longest(child, path | {node}) for child in children.get(node, ())
                         if child not in path and child != node), default=0)
        memo[node] = depth
        return depth

    roots = [k for _, w, v in root_refs if (k := key(w, v))]
    return max((longest(node, frozenset()) for node in roots), default=0)


def closure(resolver: CallResolver, root_refs: list[Ref], root: str) -> tuple[list[ResolvedCall], list[SpecError]]:
    """Every workflow reachable from ``root_refs``, plus errors, with the graph bounded in size and depth."""
    bounded = _Bounded(resolver)
    try:
        found, errors = call_graph_refs(root_refs, bounded, root)
    except GraphTooLarge as exc:
        return [], [SpecError(code=TOO_LARGE, message=str(exc))]
    if _depth(found, root_refs, resolver) > MAX_DEPTH:
        errors.append(SpecError(code=TOO_LARGE, message=f"Calls nest at most {MAX_DEPTH} levels deep."))
    return found, errors


def pin(conn: Connection, scope: str, body: str | None, root: str) -> tuple[dict[str, int], list[str]]:
    """The pins and the callees' bodies for a job queued now. Raises ValueError when the calls don't resolve."""
    root_refs = body_refs(body)
    if not root_refs:
        return {}, []
    resolver = DbCallResolver(conn, scope)
    found, errors = closure(resolver, root_refs, root)
    if errors:
        raise ValueError("; ".join(f"{e.code}: {e.message}" for e in errors))
    return dict(sorted(resolver.resolved.items())), [c.text for c in found]


def bundle(conn: Connection, scope: str, body: str, root: str, pins: dict[str, int] | None,
           stored: Any) -> dict[str, str]:
    """The call bundle's files, manifest.json and <workflow>@<version>.yaml, for a job. Raises ValueError."""
    root_refs = body_refs(body)
    if not root_refs:
        return {}
    resolver = DbCallResolver(conn, scope, pins)
    found, errors = closure(resolver, root_refs, root)
    if errors:
        raise ValueError("; ".join(f"{e.code}: {e.message}" for e in errors))
    files = {bundle_file_name(c.workflow, c.version): stored(c.text) for c in found}
    files[MANIFEST] = json.dumps({"pins": dict(sorted(resolver.resolved.items()))}, indent=2) + "\n"
    return files


def callers(conn: Connection, scope: str, name: str, *, exclude: str | None = None) -> list[str]:
    """The workflows of ``scope`` whose current version calls ``name``."""
    rows = conn.execute(select(workflows.c.id, workflows.c.name, versions.c.body)
                        .join(versions, (versions.c.object_kind == "workflow")
                              & (versions.c.object_id == workflows.c.id)
                              & (versions.c.version == workflows.c.current_version))
                        .where(workflows.c.scope == scope)).all()
    return sorted(r.name for r in rows if r.id != exclude and any(w == name for _, w, _ in body_refs(r.body)))
