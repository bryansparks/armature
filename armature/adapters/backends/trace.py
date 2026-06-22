"""Trace-based adapter backend.

Converts exported SFT/DPO trace JSONL into a LoRA adapter. This closes the v2
fine-tuning flywheel: accumulated high-quality traces are distilled back into
behavioral adapters for the role types or stages that produced them.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from armature.adapters.backends.trainer import MockTrainer, Trainer
from armature.adapters.data import TrainingDataset, TrainingExample
from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


class TraceAdapterFactory(AdapterFactory):
    """Train a LoRA adapter from exported SFT/DPO trace JSONL."""

    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        trainer: Trainer | None = None,
    ) -> None:
        self._registry = registry or AdapterRegistry()
        self._trainer = trainer or MockTrainer()

    def available(self) -> bool:
        return self._trainer.available()

    async def submit(self, request: AdapterRequest) -> AdapterJob:
        if request.traces_path is None:
            raise ValueError("TraceAdapterFactory requires request.traces_path")
        job_id = f"trace-{uuid.uuid4().hex[:8]}"
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
            backend="trace",
            job_id=job_id,
        )
        return AdapterJob(
            job_id=job_id,
            backend="trace",
            status="queued",
            metadata=metadata,
            request=request,
        )

    async def poll(self, job: AdapterJob) -> AdapterJob:
        if job.status in ("done", "failed"):
            return job
        if job.status == "queued":
            job.status = "running"
            job.logs.append(f"loading traces from {job.request.traces_path if job.request else 'unknown'}")
            return job
        if job.status != "running" or job.metadata is None or job.request is None:
            job.status = "failed"
            job.logs.append("invalid job state")
            return job
        try:
            from armature.adapters.backends.continual import resolve_prior_artifact_dir

            dataset = _load_trace_dataset(job.request)
            prior_artifact_dir = resolve_prior_artifact_dir(self._registry, job.request)
            work_dir = Path(tempfile.mkdtemp(prefix="armature-trace-"))
            artifact_dir = await self._trainer.train(
                dataset,
                job.request,
                work_dir,
                prior_artifact_dir=prior_artifact_dir,
            )
            promote = job.request.extra.get("promote", True)
            self._registry.register(job.metadata, artifact_dir, promote=promote)
            job.artifact_path = self._registry.get(
                job.metadata.name, job.metadata.version
            ).artifact_dir
            job.status = "done"
            job.logs.append(
                f"registered adapter {job.metadata.name}@{job.metadata.version} "
                f"from {len(dataset.examples)} trace examples"
            )
        except Exception as exc:  # pragma: no cover - safety net
            job.status = "failed"
            job.logs.append(f"training failed: {exc}")
        return job


def _load_trace_dataset(request: AdapterRequest) -> TrainingDataset:
    """Read SFT/DPO JSONL and convert to TrainingExample objects."""
    from armature.adapters.preprocess import PreprocessConfig, preprocess_examples

    path = request.traces_path
    role_type_filter = request.extra.get("role_type")
    stage_id_filter = request.extra.get("stage_id")
    min_score = request.extra.get("min_score")

    examples: list[TrainingExample] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            # DPO format
            if "chosen" in row and "rejected" in row:
                messages = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": row.get("prompt", "")},
                    {"role": "assistant", "content": row["chosen"]},
                ]
                examples.append(
                    TrainingExample(
                        messages=messages,
                        source="trace",
                        score=row.get("score"),
                    )
                )
                continue

            # SFT chat format
            messages = row.get("messages", [])
            if not messages:
                continue

            meta = row.get("metadata", {})
            record_role = meta.get("role_type") if isinstance(meta, dict) else None
            record_stage = meta.get("stage_id") if isinstance(meta, dict) else None
            record_score = meta.get("score") if isinstance(meta, dict) else None

            if role_type_filter and record_role != role_type_filter:
                continue
            if stage_id_filter and record_stage != stage_id_filter:
                continue
            if min_score is not None and (
                record_score is None or record_score < min_score
            ):
                continue

            examples.append(
                TrainingExample(
                    messages=messages,
                    source="trace",
                    score=record_score,
                )
            )

    preprocess = request.extra.get("preprocess")
    if isinstance(preprocess, dict):
        config = PreprocessConfig(**preprocess)
        examples = preprocess_examples(examples, config)

    return TrainingDataset(
        examples=examples,
        base_model=request.base_model,
        name=request.name,
    )


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
