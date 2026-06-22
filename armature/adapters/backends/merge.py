"""Merged adapter backend.

Combines multiple existing LoRA adapters into one artifact. The initial
implementation concatenates metadata and writes a manifest of source adapters.
When real safetensors merging is available (e.g. via peft or slerp), this is
the place to wire it in; for now the merged artifact is a valid placeholder
that the registry and runtime can load.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


class MergedAdapterFactory(AdapterFactory):
    """Combine several registered adapters into a single artifact."""

    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self._registry = registry or AdapterRegistry()

    def available(self) -> bool:
        return True

    async def submit(self, request: AdapterRequest) -> AdapterJob:
        refs = request.extra.get("adapter_refs", [])
        if not refs:
            raise ValueError("MergedAdapterFactory requires extra['adapter_refs']")
        job_id = f"merge-{uuid.uuid4().hex[:8]}"
        version = _fresh_version(self._registry, request.name)
        metadata = AdapterMetadata(
            name=request.name,
            version=version,
            base_model=request.base_model,
            rank=request.rank,
            alpha=request.alpha,
            target_modules=list(request.target_modules),
            use_dora=request.use_dora,
            continual_learning=request.continual_learning,
            prior_adapter_version=request.prior_adapter_version,
            backend="merge",
            job_id=job_id,
        )
        return AdapterJob(
            job_id=job_id,
            backend="merge",
            status="queued",
            metadata=metadata,
            request=request,
        )

    async def poll(self, job: AdapterJob) -> AdapterJob:
        if job.status in ("done", "failed"):
            return job
        if job.status == "queued":
            job.status = "running"
            job.logs.append("merge started")
            return job
        if job.status != "running" or job.metadata is None or job.request is None:
            job.status = "failed"
            job.logs.append("invalid job state")
            return job
        try:
            refs = job.request.extra.get("adapter_refs", [])
            resolved = self._resolve_refs(refs)
            artifact_dir = _write_merged_artifact(job.metadata, resolved)
            self._registry.register(job.metadata, artifact_dir)
            job.artifact_path = self._registry.get(
                job.metadata.name, job.metadata.version
            ).artifact_dir
            job.status = "done"
            job.logs.append(
                f"merged {len(resolved)} adapters into {job.metadata.name}@{job.metadata.version}"
            )
        except Exception as exc:
            job.status = "failed"
            job.logs.append(f"merge failed: {exc}")
        return job

    def _resolve_refs(self, refs: list[str]) -> list[tuple[str, str, Path]]:
        """Resolve name@version strings into (name, version, artifact_dir)."""
        resolved: list[tuple[str, str, Path]] = []
        for ref in refs:
            if "@" not in ref:
                raise ValueError(f"Adapter reference must be name@version, got {ref!r}")
            name, version = ref.split("@", 1)
            r = self._registry.get(name, version)
            resolved.append((name, version, r.artifact_dir))
        return resolved


def _write_merged_artifact(
    metadata: AdapterMetadata,
    sources: list[tuple[str, str, Path]],
) -> Path:
    """Create a merged adapter directory with a manifest of sources."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="armature-merge-"))
    config = {
        "lora_alpha": metadata.alpha,
        "r": metadata.rank,
        "target_modules": metadata.target_modules,
        "base_model_name_or_path": metadata.base_model,
        "use_dora": metadata.use_dora,
        "continual_learning": metadata.continual_learning,
        "prior_adapter_version": metadata.prior_adapter_version,
        "merged_from": [
            {"name": name, "version": version, "path": str(path)}
            for name, version, path in sources
        ],
    }
    (tmp_dir / "adapter_config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    # Copy the first source's safetensors as a placeholder. A real merge would
    # combine weights mathematically here.
    if sources:
        src_weights = sources[0][2] / "adapter.safetensors"
        if src_weights.exists():
            shutil.copy(src_weights, tmp_dir / "adapter.safetensors")
        else:
            (tmp_dir / "adapter.safetensors").write_bytes(b"MERGE")
    else:
        (tmp_dir / "adapter.safetensors").write_bytes(b"MERGE")
    return tmp_dir


def _fresh_version(registry: AdapterRegistry, name: str) -> str:
    try:
        latest = registry._manifest(name).latest_version()
    except ValueError:
        latest = None
    if latest is None:
        return "1"
    try:
        return str(int(latest) + 1)
    except ValueError:
        return f"{latest}-next"
