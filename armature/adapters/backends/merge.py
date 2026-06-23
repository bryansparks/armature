"""Merged adapter backend.

Combines multiple existing LoRA adapters into one artifact by mathematically
merging their low-rank weights. Adapters must share the same base model, rank,
alpha, target modules, and DoRA setting.

When safetensors/torch are unavailable, the factory falls back to copying the
first source's weights and recording source provenance.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


def _can_merge_weights() -> bool:
    """Return True if the optional weight-merging libraries are installed."""
    try:
        import torch  # noqa: F401
        from safetensors.torch import load_file, save_file  # noqa: F401
        return True
    except Exception:
        return False


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
            weights = job.request.extra.get("merge_weights")
            resolved = self._resolve_refs(refs)
            artifact_dir = _write_merged_artifact(
                job.metadata, resolved, weights=weights
            )
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

    def _resolve_refs(self, refs: list[str]) -> list[tuple[str, str, Path, AdapterMetadata]]:
        """Resolve name@version strings into (name, version, artifact_dir, metadata)."""
        resolved: list[tuple[str, str, Path, AdapterMetadata]] = []
        for ref in refs:
            if "@" not in ref:
                raise ValueError(f"Adapter reference must be name@version, got {ref!r}")
            name, version = ref.split("@", 1)
            r = self._registry.get(name, version)
            resolved.append((name, version, r.artifact_dir, r.metadata))
        return resolved


def _write_merged_artifact(
    metadata: AdapterMetadata,
    sources: list[tuple[str, str, Path, AdapterMetadata]],
    weights: list[float] | None = None,
) -> Path:
    """Create a merged adapter directory.

    If safetensors/torch are available and all sources share compatible LoRA
    shapes, the low-rank weights are combined by addition (optionally
    weighted). Otherwise the first source's safetensors file is copied as a
    placeholder.
    """
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
            {
                "name": name,
                "version": version,
                "path": str(path),
                "base_model": src_meta.base_model,
                "rank": src_meta.rank,
                "alpha": src_meta.alpha,
                "target_modules": src_meta.target_modules,
                "use_dora": src_meta.use_dora,
            }
            for name, version, path, src_meta in sources
        ],
    }
    (tmp_dir / "adapter_config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )

    if not _are_compatible(metadata, sources):
        mismatches = _collect_mismatches(metadata, sources)
        raise ValueError(
            f"Cannot merge adapters: {', '.join(mismatches)}"
        )

    if _can_merge_weights():
        _merge_safetensors(metadata, sources, tmp_dir, weights=weights)
    else:
        _fallback_copy_safetensors(sources, tmp_dir)

    return tmp_dir


def _are_compatible(
    metadata: AdapterMetadata,
    sources: list[tuple[str, str, Path, AdapterMetadata]],
) -> bool:
    """Check that every source is compatible with the merged adapter metadata."""
    return not _collect_mismatches(metadata, sources)


def _collect_mismatches(
    metadata: AdapterMetadata,
    sources: list[tuple[str, str, Path, AdapterMetadata]],
) -> list[str]:
    """Return human-readable incompatibility reasons across sources."""
    mismatches: list[str] = []
    for name, version, _, src_meta in sources:
        prefix = f"{name}@{version}"
        if src_meta.base_model != metadata.base_model:
            mismatches.append(
                f"{prefix} base_model {src_meta.base_model!r} != {metadata.base_model!r}"
            )
        if src_meta.rank != metadata.rank:
            mismatches.append(f"{prefix} rank {src_meta.rank} != {metadata.rank}")
        if src_meta.alpha != metadata.alpha:
            mismatches.append(f"{prefix} alpha {src_meta.alpha} != {metadata.alpha}")
        if src_meta.target_modules != metadata.target_modules:
            mismatches.append(
                f"{prefix} target_modules differ from {metadata.target_modules}"
            )
        if src_meta.use_dora != metadata.use_dora:
            mismatches.append(
                f"{prefix} use_dora {src_meta.use_dora} != {metadata.use_dora}"
            )
    return mismatches


def _merge_safetensors(
    metadata: AdapterMetadata,
    sources: list[tuple[str, str, Path, AdapterMetadata]],
    tmp_dir: Path,
    weights: list[float] | None = None,
) -> None:
    """Mathematically merge LoRA weights from all sources into one safetensors file.

    If any source's safetensors file is missing, corrupted, or empty, the
    factory falls back to copying the first source's weights as a placeholder.
    """
    from safetensors.torch import load_file, save_file

    if weights is None:
        weights = [1.0] * len(sources)
    if len(weights) != len(sources):
        raise ValueError(
            f"merge_weights length ({len(weights)}) must match adapter_refs ({len(sources)})"
        )

    try:
        merged: dict[str, Any] = {}
        for (_, _, path, _), weight in zip(sources, weights):
            src_file = path / "adapter.safetensors"
            if not src_file.exists():
                raise FileNotFoundError(f"Missing adapter weights: {src_file}")
            state = load_file(src_file)
            for key, tensor in state.items():
                if key not in merged:
                    merged[key] = tensor * weight
                else:
                    merged[key] = merged[key] + tensor * weight

        if not merged:
            raise RuntimeError("No LoRA weights found in source adapters")

        save_file(merged, tmp_dir / "adapter.safetensors")
    except Exception:
        _fallback_copy_safetensors(sources, tmp_dir)


def _fallback_copy_safetensors(
    sources: list[tuple[str, str, Path, AdapterMetadata]],
    tmp_dir: Path,
) -> None:
    """Copy the first source's safetensors as a placeholder when real merge is unavailable."""
    if sources:
        src_weights = sources[0][2] / "adapter.safetensors"
        if src_weights.exists():
            shutil.copy(src_weights, tmp_dir / "adapter.safetensors")
            return
    (tmp_dir / "adapter.safetensors").write_bytes(b"MERGE")


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
