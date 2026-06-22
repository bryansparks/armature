"""Adapter manifest I/O.

The registry stores one manifest per adapter name. Each manifest records
metadata for every version of that adapter, plus the pointer to the
`latest` version.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AdapterMetadata:
    """Immutable metadata describing one adapter version."""

    name: str
    version: str
    base_model: str
    rank: int = 16
    alpha: int = 32
    target_modules: list[str] = field(default_factory=lambda: ["q_proj", "v_proj"])
    training_data_hash: str | None = None
    validation_score: float | None = None
    created_at: str | None = None
    backend: str | None = None
    job_id: str | None = None


class Manifest:
    """On-disk manifest for a single adapter name."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {"versions": {}, "latest": None}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def versions(self) -> dict[str, AdapterMetadata]:
        """Return all versions keyed by version string."""
        return {
            version: AdapterMetadata(**data)
            for version, data in self._data.get("versions", {}).items()
        }

    def latest_version(self) -> str | None:
        return self._data.get("latest")

    def set_latest(self, version: str) -> None:
        if version not in self._data.get("versions", {}):
            raise ValueError(f"Cannot promote unknown version '{version}'")
        self._data["latest"] = version
        self._save()

    def add(self, metadata: AdapterMetadata) -> None:
        if "versions" not in self._data:
            self._data["versions"] = {}
        self._data["versions"][metadata.version] = asdict(metadata)
        if self._data.get("latest") is None:
            self._data["latest"] = metadata.version
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
