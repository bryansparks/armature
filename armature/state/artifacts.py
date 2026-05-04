from __future__ import annotations
import json
from pathlib import Path
from typing import Any


class ArtifactStore:
    def __init__(self, base_dir: Path | str):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str, ext: str = "json") -> Path:
        return self._base / f"{name}.{ext}"

    async def write(self, name: str, data: Any) -> Path:
        path = self._path(name, "json")
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    async def read(self, name: str) -> Any | None:
        path = self._path(name, "json")
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    async def write_text(self, name: str, content: str) -> Path:
        path = self._path(name, "md")
        path.write_text(content, encoding="utf-8")
        return path

    async def read_text(self, name: str) -> str | None:
        for ext in ("md", "txt"):
            path = self._path(name, ext)
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    async def list(self) -> list[str]:
        return [p.stem for p in self._base.glob("*") if p.is_file()]
