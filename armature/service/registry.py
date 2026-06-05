from __future__ import annotations
from pathlib import Path
from typing import Any

from armature.spec.loader import load_spec
from armature.spec.models import HarnessSpec


class WorkflowRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, HarnessSpec] = {}

    def load_dir(self, specs_dir: Path) -> None:
        """Load all *.yaml / *.yml specs from a directory, keyed by spec.name."""
        for path in sorted(specs_dir.glob("*.yaml")) + sorted(specs_dir.glob("*.yml")):
            try:
                spec = load_spec(path)
                self._specs[spec.name] = spec
            except Exception:
                pass  # malformed spec — skip silently

    def register(self, spec: HarnessSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> HarnessSpec | None:
        return self._specs.get(name)

    def list_all(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "description": s.description, "stages": len(s.stages)}
            for s in self._specs.values()
        ]
