"""Workflow packs: versioned directories of script adapters a step names as ``pack/adapter``.

A pack holds a ``pack.yaml`` and the scripts it names. The catalog searches
the built-in packs shipped in this package, each entry of
``ARMATURE_PACKS_PATH``, and then ``~/.armature/packs``. An entry is either
one pack or a directory of packs. The first pack loaded under a name wins, so
no installed pack can replace a built-in one, and every later copy is listed
in ``Catalog.errors``.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from armature.spec.models import PACK_ADAPTER_CONTEXT, Adapter

PACKS_PATH_ENV = "ARMATURE_PACKS_PATH"
DEFAULT_PACKS_DIR = Path("~/.armature/packs")
PACK_FILE = "pack.yaml"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
BUILTIN_PACKS = ("conversation", "github", "jira", "linear", "review", "slack")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ADAPTER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SECRET_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
TEMPLATE_MARKERS = ("{{", "{%")


class PackError(ValueError):
    """A pack directory or ``pack.yaml`` that cannot be loaded."""


def version_key(version: str) -> tuple:
    """Sort key for a semver string, with a prerelease ordered before its release."""
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError(f"not a semantic version: {version!r}")
    major, minor, patch, pre = int(match[1]), int(match[2]), int(match[3]), match[4]
    if pre is None:
        return (major, minor, patch, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (major, minor, patch, 0, parts)


class AdapterDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    cmd: str
    params: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    outputs: dict[str, Any] | None = None
    timeout: int = Field(60, ge=1)
    writes: bool = False
    secrets: list[str] = Field(default_factory=list)

    @field_validator("cmd")
    @classmethod
    def _static_cmd(cls, value: str) -> str:
        if any(marker in value for marker in TEMPLATE_MARKERS):
            raise ValueError("cmd must not contain template markers ('{{' or '{%')")
        if not value.strip():
            raise ValueError("cmd must not be empty")
        return value

    @field_validator("params")
    @classmethod
    def _params_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"params is not a valid JSON Schema: {exc.message}") from exc
        if value.get("type") != "object":
            raise ValueError("params must be a JSON Schema with type: object")
        return value

    @field_validator("outputs")
    @classmethod
    def _outputs_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            try:
                Draft202012Validator.check_schema(value)
            except SchemaError as exc:
                raise ValueError(f"outputs is not a valid JSON Schema: {exc.message}") from exc
        return value

    @field_validator("secrets")
    @classmethod
    def _secret_names(cls, value: list[str]) -> list[str]:
        bad = [name for name in value if not SECRET_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"secret names must match {SECRET_RE.pattern}: {bad}")
        return value

    def to_adapter(self, ref: str, pack_dir: Path) -> Adapter:
        return Adapter.model_validate(
            {"name": ref, "type": "script", "cmd": self.cmd, "timeout": self.timeout, "parse": "json",
             "pack_dir": str(pack_dir), "params": self.params},
            context={PACK_ADAPTER_CONTEXT: True},
        )


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    tools_allowlist: list[str] = Field(default_factory=list)
    mcp_allowlist: list[str] = Field(default_factory=list)
    adapters: dict[str, AdapterDef] = Field(default_factory=dict)
    dir: Path | None = Field(None, exclude=True)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME_RE.fullmatch(value):
            raise ValueError(f"pack name must match {NAME_RE.pattern}")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not SEMVER_RE.fullmatch(value):
            raise ValueError("version must be a semantic version such as 1.0.0")
        return value

    @field_validator("adapters")
    @classmethod
    def _adapter_names(cls, value: dict[str, AdapterDef]) -> dict[str, AdapterDef]:
        bad = [name for name in value if not ADAPTER_RE.fullmatch(name)]
        if bad:
            raise ValueError(f"adapter names must match {ADAPTER_RE.pattern}: {bad}")
        return value

    def ref(self, adapter: str) -> str:
        return f"{self.name}/{adapter}"

    @property
    def builtin(self) -> bool:
        return is_builtin(self.dir)


def is_builtin(pack_dir: Path | str | None) -> bool:
    """Whether ``pack_dir`` is one of the packs shipped in this package."""
    return pack_dir is not None and Path(pack_dir).resolve().parent == BUILTIN_DIR \
        and Path(pack_dir).name in BUILTIN_PACKS


def load_pack(directory: Path | str) -> Pack:
    """Load and validate ``<directory>/pack.yaml``. Raises ``PackError``, whose message names no path."""
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    directory = Path(directory).expanduser().resolve()
    manifest = directory / PACK_FILE
    if not manifest.is_file():
        raise PackError(f"no {PACK_FILE}")
    try:
        data = YAML(typ="safe").load(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PackError(f"{PACK_FILE} is unreadable: {exc.__class__.__name__}") from exc
    except YAMLError as exc:
        raise PackError(f"{PACK_FILE} does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError(f"{PACK_FILE} must hold a mapping")
    data.pop("dir", None)
    try:
        pack = Pack.model_validate(data)
    except ValidationError as exc:
        raise PackError(f"{PACK_FILE}: {_validation_message(exc)}") from exc
    pack.dir = directory
    return pack


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()
    )


@dataclass(frozen=True)
class CatalogError:
    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class Catalog:
    _packs: dict[str, Pack] = field(default_factory=dict)
    errors: list[CatalogError] = field(default_factory=list)

    def packs(self) -> list[Pack]:
        return [self._packs[name] for name in sorted(self._packs)]

    def pack(self, name: str) -> Pack | None:
        return self._packs.get(name)

    def resolve(self, ref: str) -> tuple[Pack, AdapterDef] | None:
        pack_name, sep, adapter_name = ref.partition("/")
        pack = self._packs.get(pack_name) if sep else None
        if pack is None or adapter_name not in pack.adapters:
            return None
        return pack, pack.adapters[adapter_name]

    def get(self, ref: str) -> AdapterDef | None:
        found = self.resolve(ref)
        return found[1] if found else None

    def tools_allowlist(self) -> list[str]:
        return _union(pack.tools_allowlist for pack in self.packs())

    def mcp_allowlist(self) -> list[str]:
        return _union(pack.mcp_allowlist for pack in self.packs())


def _union(lists: Iterable[list[str]]) -> list[str]:
    seen: dict[str, None] = {}
    for items in lists:
        for item in items:
            seen.setdefault(item, None)
    return list(seen)


def search_path(env: Mapping[str, str] | None = None, default_dir: Path | str | None = None) -> list[Path]:
    env = os.environ if env is None else env
    entries = [Path(p).expanduser() for p in env.get(PACKS_PATH_ENV, "").split(os.pathsep) if p]
    return [BUILTIN_DIR, *entries, Path(default_dir or DEFAULT_PACKS_DIR).expanduser()]


def pack_dirs(root: Path) -> list[Path]:
    """The pack directories under one search-path entry: itself, or its children with a pack.yaml."""
    if (root / PACK_FILE).is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir()
                  if not child.name.startswith(".") and (child / PACK_FILE).is_file())


def catalog(paths: Iterable[Path | str] | None = None) -> Catalog:
    """Load every pack on ``paths`` (default ``search_path()``)."""
    roots = search_path() if paths is None else [Path(p).expanduser() for p in paths]
    result = Catalog()
    for root in roots:
        for directory in pack_dirs(root):
            try:
                pack = load_pack(directory)
            except PackError as exc:
                result.errors.append(CatalogError(directory, str(exc)))
                continue
            first = result._packs.get(pack.name)
            if first is not None:
                result.errors.append(CatalogError(
                    directory, f"duplicate pack '{pack.name}', already loaded from an earlier entry, is ignored"))
                continue
            result._packs[pack.name] = pack
    return result
