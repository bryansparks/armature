"""Mock adapter factory for tests and CI.

Produces deterministic, tiny LoRA-shaped artifacts without loading any ML
frameworks. Useful for validating the adapter lifecycle end-to-end without a
GPU or heavy dependencies.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


class MockAdapterFactory(AdapterFactory):
    """Backend that creates dummy adapter artifacts instantly."""

    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self._registry = registry or AdapterRegistry()

    def available(self) -> bool:
        return True

    async def submit(self, request: AdapterRequest) -> AdapterJob:
        job_id = f"mock-{uuid.uuid4().hex[:8]}"
        metadata = AdapterMetadata(
            name=request.name,
            version=_fresh_version(self._registry, request.name),
            base_model=request.base_model,
            rank=request.rank,
            alpha=request.alpha,
            target_modules=list(request.target_modules),
            use_dora=request.use_dora,
            continual_learning=request.continual_learning,
            prior_adapter_version=request.prior_adapter_version,
            backend="mock",
            job_id=job_id,
        )
        return AdapterJob(
            job_id=job_id,
            backend="mock",
            status="queued",
            metadata=metadata,
            request=request,
        )

    async def poll(self, job: AdapterJob) -> AdapterJob:
        if job.status in ("done", "failed"):
            return job

        if job.status == "queued":
            job.status = "running"
            job.logs.append("mock training started")
            return job

        if job.status == "running":
            if job.metadata is None:
                job.status = "failed"
                job.logs.append("mock training failed: missing metadata")
                return job
            try:
                artifact_dir = _write_dummy_artifact(job.metadata)
                self._registry.register(job.metadata, artifact_dir)
                job.artifact_path = self._registry.get(
                    job.metadata.name, job.metadata.version
                ).artifact_dir
                job.status = "done"
                job.logs.append("mock training completed")
            except Exception as exc:  # pragma: no cover - safety net
                job.status = "failed"
                job.logs.append(f"mock training failed: {exc}")
        return job


def _fresh_version(registry: AdapterRegistry, name: str) -> str:
    """Return the next version string for an adapter name (1, 2, 3...)."""
    try:
        latest = registry._manifest(name).latest_version()
    except ValueError:
        latest = None
    if latest is None:
        return "1"
    try:
        return str(int(latest) + 1)
    except ValueError:
        # Non-numeric versions fall back to appending a timestamp suffix.
        return f"{latest}-next"


def _write_dummy_artifact(metadata: AdapterMetadata) -> Path:
    """Create a minimal LoRA-shaped directory in a temp location."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="armature-mock-adapter-"))
    config = {
        "lora_alpha": metadata.alpha,
        "r": metadata.rank,
        "target_modules": metadata.target_modules,
        "base_model_name_or_path": metadata.base_model,
        "use_dora": metadata.use_dora,
        "continual_learning": metadata.continual_learning,
        "prior_adapter_version": metadata.prior_adapter_version,
    }
    (tmp_dir / "adapter_config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    # A real safetensors file starts with a specific header; this is only a
    # placeholder so that registry layout checks pass.
    (tmp_dir / "adapter.safetensors").write_bytes(b"MOCK")
    return tmp_dir
