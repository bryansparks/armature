"""Resolving the stored workflows that `call:` stages run.

The engine never reads the server database. Whoever prepares a run resolves
every called workflow first and hands the engine a CallResolver. The default,
BundleCallResolver, reads a call bundle materialized next to the top-level
workflow file::

    calls/
      manifest.json          {"pins": {"<workflow>": <version>, ...}}
      <workflow>@<version>.yaml

A `call:` with an explicit `version:` reads that file. A call without one
reads the version the manifest pins for the workflow name.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from armature.spec.models import HarnessSpec
    from armature.spec.validator import SpecError

CALLS_DIR = "calls"
MANIFEST = "manifest.json"
WORKFLOW_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class CallNotFound(LookupError):
    """The resolver has no workflow for a call's name and version."""


@dataclass(frozen=True)
class ResolvedCall:
    workflow: str
    version: int
    text: str                    # the callee spec, ideally with the stored-workflow header
    base_dir: Path | None = None  # where the callee's relative file refs resolve


@runtime_checkable
class CallResolver(Protocol):
    def resolve(self, workflow: str, version: int | None) -> ResolvedCall:
        """The callee for ``workflow`` at ``version`` (None means the pinned version). Raises CallNotFound."""
        ...


def bundle_file_name(workflow: str, version: int) -> str:
    return f"{workflow}@{version}.yaml"


def valid_workflow_name(workflow: str) -> bool:
    return bool(WORKFLOW_NAME_RE.fullmatch(workflow)) and ".." not in workflow


class BundleCallResolver:
    """Reads called workflows from a materialized call bundle directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @classmethod
    def for_spec(cls, spec: "HarnessSpec") -> "CallResolver":
        """The bundle beside the file ``spec`` was loaded from; a spec built in memory has none."""
        if not spec._source_dir:
            return NoCallResolver(spec.name)
        return cls(Path(spec._source_dir) / CALLS_DIR)

    def pins(self) -> dict[str, int]:
        path = self.root / MANIFEST
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        pins = data.get("pins") if isinstance(data, dict) else None
        return {str(k): int(v) for k, v in pins.items()} if isinstance(pins, dict) else {}

    def resolve(self, workflow: str, version: int | None) -> ResolvedCall:
        if not valid_workflow_name(workflow):
            raise CallNotFound(f"'{workflow}' is not a valid workflow name")
        if version is None:
            version = self.pins().get(workflow)
            if version is None:
                raise CallNotFound(
                    f"workflow '{workflow}' has no pinned version in {self.root / MANIFEST}"
                )
        path = self.root / bundle_file_name(workflow, version)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise CallNotFound(f"workflow '{workflow}' version {version} is not a plain file in {self.root}")
        if not path.is_file():
            raise CallNotFound(f"workflow '{workflow}' version {version} is not in the call bundle at {self.root}")
        return ResolvedCall(workflow, version, path.read_text(encoding="utf-8"), self.root)


class NoCallResolver:
    """Resolves nothing: the spec has no file, so there is no bundle beside it."""

    def __init__(self, spec_name: str):
        self._spec_name = spec_name

    def resolve(self, workflow: str, version: int | None) -> ResolvedCall:
        raise CallNotFound(
            f"workflow '{self._spec_name}' was not loaded from a file, so it has no call bundle "
            f"to resolve '{workflow}' from; pass call_resolver"
        )


class MappingCallResolver:
    """Resolves from an in-memory {(workflow, version): text} map plus pins, for tests and dry runs."""

    def __init__(self, specs: dict[tuple[str, int], str], pins: dict[str, int] | None = None,
                 base_dir: Path | None = None):
        self._specs = dict(specs)
        self._pins = dict(pins or {})
        self._base_dir = base_dir

    def resolve(self, workflow: str, version: int | None) -> ResolvedCall:
        if version is None:
            version = self._pins.get(workflow)
        text = self._specs.get((workflow, version)) if version is not None else None
        if text is None:
            raise CallNotFound(f"workflow '{workflow}' version {version} is not available")
        return ResolvedCall(workflow, version, text, self._base_dir)


def write_call_bundle(root: Path | str, calls: Iterable[ResolvedCall], pins: dict[str, int]) -> Path:
    """Materialize a call bundle at ``root`` that BundleCallResolver reads back."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for call in calls:
        if not valid_workflow_name(call.workflow):
            raise ValueError(f"'{call.workflow}' is not a valid workflow name")
        (root / bundle_file_name(call.workflow, call.version)).write_text(call.text, encoding="utf-8")
    (root / MANIFEST).write_text(json.dumps({"pins": dict(sorted(pins.items()))}, indent=2) + "\n",
                                 encoding="utf-8")
    return root


def load_callee(resolved: ResolvedCall, vars: dict | None = None) -> "HarnessSpec":
    from armature.spec.loader import load_child_spec_text

    base_dir = resolved.base_dir if resolved.base_dir is not None else Path.cwd()
    return load_child_spec_text(resolved.text, base_dir,
                                bundle_file_name(resolved.workflow, resolved.version), vars)


def call_graph(spec: "HarnessSpec", resolver: CallResolver) -> tuple[list[ResolvedCall], list["SpecError"]]:
    """Every workflow reachable through `call:` from ``spec``, plus errors for missing callees and cycles.

    The server calls this when it builds a bundle; validate_spec calls it when given a resolver.
    """
    refs = [(s.id, s.call.workflow, s.call.version) for s in spec.stages if s.call is not None]
    return call_graph_refs(refs, resolver, spec.name)


def call_graph_refs(refs: list[tuple[str, str, int | None]], resolver: CallResolver,
                    root: str) -> tuple[list[ResolvedCall], list["SpecError"]]:
    """call_graph for raw (stage_id, workflow, version) refs made by the workflow named ``root``."""
    from armature.spec.validator import SpecError

    found: dict[tuple[str, int], ResolvedCall] = {}
    errors: list[SpecError] = []
    reported: set[tuple[str, ...]] = set()

    def walk(level_refs: list[tuple[str, str, int | None]], path: tuple[str, ...]) -> None:
        for stage_id, workflow, version in level_refs:
            try:
                resolved = resolver.resolve(workflow, version)
            except CallNotFound as exc:
                errors.append(SpecError(code="CALL_UNRESOLVED", message=str(exc), stage_id=stage_id))
                continue
            key = f"{resolved.workflow}@{resolved.version}"
            if resolved.workflow in {p.split("@")[0] for p in path}:
                cycle = (*path, key)
                if cycle not in reported:
                    reported.add(cycle)
                    errors.append(SpecError(
                        code="CALL_CYCLE", message="call cycle: " + " -> ".join(cycle), stage_id=stage_id,
                    ))
                continue
            if (resolved.workflow, resolved.version) in found:
                continue
            found[(resolved.workflow, resolved.version)] = resolved
            try:
                callee = load_callee(resolved)
            except Exception as exc:
                errors.append(SpecError(
                    code="CALL_INVALID_CALLEE", message=f"workflow '{key}' does not load: {exc}",
                    stage_id=stage_id,
                ))
                continue
            walk([(s.id, s.call.workflow, s.call.version) for s in callee.stages if s.call is not None],
                 (*path, key))

    walk(refs, (root,))
    return list(found.values()), errors
