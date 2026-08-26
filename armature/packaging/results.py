# armature/packaging/results.py
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from armature.packaging.manifest import (
    Destinations, ResultsManifest, ArtifactResult, TraceRef,
)

_EXT = {"markdown": "md", "json": "json", "text": "txt"}


class ResultsWriter:
    def __init__(self, base_dir: Path | str):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def write(self, *, run_id: str, package_name: str, package_version: str,
              destinations: Destinations, result: dict[str, Any],
              trace_records: list, status: str, started_at: str, finished_at: str,
              duration_s: float, exit_code: int, armature_version: str,
              error: str | None = None) -> Path:
        run_dir = self._base / run_id
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)

        artifact_results: list[ArtifactResult] = []
        for a in destinations.artifacts:
            stage_out = result.get(a.stage_id, {})
            content = self._extract(stage_out, a.format)
            ext = _EXT[a.format]
            path = f"artifacts/{a.name}.{ext}"
            fpath = run_dir / path
            if a.format == "json":
                fpath.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")
            else:
                fpath.write_text(content if isinstance(content, str) else str(content),
                                 encoding="utf-8")
            artifact_results.append(ArtifactResult(name=a.name, stage_id=a.stage_id,
                                                    format=a.format, path=path))

        (run_dir / "result.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8")

        trace_ref = TraceRef(included=False)
        if destinations.include_trace:
            tpath = run_dir / "trace.jsonl"
            with tpath.open("w", encoding="utf-8") as fh:
                for r in trace_records:
                    obj = r.model_dump() if hasattr(r, "model_dump") else dict(r)
                    fh.write(json.dumps(obj, default=str) + "\n")
            trace_ref = TraceRef(included=True, path="trace.jsonl")

        receipt = ResultsManifest(
            package_name=package_name, package_version=package_version, run_id=run_id,
            status=status, started_at=started_at, finished_at=finished_at,
            duration_s=duration_s, exit_code=exit_code, armature_version=armature_version,
            artifacts=artifact_results, trace=trace_ref, error=error,
        )
        (run_dir / "receipt.json").write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
        return run_dir

    @staticmethod
    def _extract(stage_out: Any, fmt: str) -> Any:
        if fmt == "json":
            return stage_out if isinstance(stage_out, dict) else {"value": stage_out}
        if isinstance(stage_out, dict) and "content" in stage_out:
            return stage_out["content"]
        if isinstance(stage_out, str):
            return stage_out
        return json.dumps(stage_out, indent=2, default=str) if stage_out else ""