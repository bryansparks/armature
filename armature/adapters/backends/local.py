"""Local training backend.

Wraps any available local LoRA trainer (PEFT, mlx-lora, unsloth) and runs the
fine-tuning job on the local machine. If no ML library is installed, the
factory reports unavailable and falls back to the mock trainer for tests.
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from armature.adapters.backends.trainer import Trainer
from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


def _discover_trainer() -> Trainer | None:
    """Return the first available local trainer implementation, if any."""
    try:
        from armature.adapters.backends.trainer_peft import PEFTLoraTrainer

        if PEFTLoraTrainer().available():
            return PEFTLoraTrainer()
    except Exception:
        pass
    try:
        from armature.adapters.backends.trainer_mlx import MLXLoraTrainer

        if MLXLoraTrainer().available():
            return MLXLoraTrainer()
    except Exception:
        pass
    try:
        from armature.adapters.backends.trainer_unsloth import UnslothLoraTrainer

        if UnslothLoraTrainer().available():
            return UnslothLoraTrainer()
    except Exception:
        pass
    return None


class LocalAdapterFactory(AdapterFactory):
    """Run adapter training locally using an installed LoRA trainer."""

    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        trainer: Trainer | None = None,
    ) -> None:
        self._registry = registry or AdapterRegistry()
        self._trainer = trainer or _discover_trainer()

    def available(self) -> bool:
        return self._trainer is not None and self._trainer.available()

    async def submit(self, request: AdapterRequest) -> AdapterJob:
        job_id = f"local-{uuid.uuid4().hex[:8]}"
        version = _fresh_version(self._registry, request.name)
        metadata = AdapterMetadata(
            name=request.name,
            version=version,
            base_model=request.base_model,
            rank=request.rank,
            alpha=request.alpha,
            target_modules=list(request.target_modules),
            backend="local",
            job_id=job_id,
        )
        return AdapterJob(
            job_id=job_id,
            backend="local",
            status="queued",
            metadata=metadata,
            request=request,
        )

    async def poll(self, job: AdapterJob) -> AdapterJob:
        if job.status in ("done", "failed"):
            return job
        if job.status == "queued":
            if self._trainer is None or not self._trainer.available():
                job.status = "failed"
                job.logs.append("no local LoRA trainer available")
                return job
            job.status = "running"
            job.logs.append(f"local training started with {type(self._trainer).__name__}")
            return job
        if job.status != "running" or job.metadata is None or job.request is None:
            job.status = "failed"
            job.logs.append("invalid job state")
            return job
        try:
            dataset = _materialize_dataset(job.request)
            work_dir = Path(tempfile.mkdtemp(prefix="armature-local-"))
            artifact_dir = await self._trainer.train(dataset, job.request, work_dir)
            self._registry.register(job.metadata, artifact_dir)
            job.artifact_path = self._registry.get(
                job.metadata.name, job.metadata.version
            ).artifact_dir
            job.status = "done"
            job.logs.append(
                f"registered adapter {job.metadata.name}@{job.metadata.version}"
            )
        except Exception as exc:
            job.status = "failed"
            job.logs.append(f"local training failed: {exc}")
        return job


def _materialize_dataset(request: AdapterRequest) -> TrainingDataset:
    """Convert a request into a TrainingDataset.

    Skill-based requests are synthesized by the S2L backend; trace-based
    requests are loaded from disk. Both reuse the same trainer interface.
    """
    if request.skill is not None:
        from armature.adapters.backends.s2l import _synthesize_dataset

        return _synthesize_dataset(request.skill, request)
    if request.traces_path is not None:
        from armature.adapters.backends.trace import _load_trace_dataset

        return _load_trace_dataset(request)
    raise ValueError("AdapterRequest must provide skill or traces_path")


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
