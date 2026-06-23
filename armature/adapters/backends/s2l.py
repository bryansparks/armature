"""Skill-to-LoRA (S2L) adapter backend.

Converts a single :class:`armature.spec.models.SkillDef` into a LoRA adapter by
synthesizing supervised fine-tuning examples from the skill document. This is a
lightweight, behavior-centric approximation of the S2L paper's self-distillation
idea: the skill text itself acts as the teacher output, and we generate a small
variety of user prompts that would plausibly invoke the skill.
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from armature.adapters.backends.trainer import MockTrainer, Trainer
from armature.adapters.data import TrainingDataset, TrainingExample
from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry


class S2LSkillAdapterFactory(AdapterFactory):
    """Train a LoRA adapter from one skill document."""

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
        if request.skill is None:
            raise ValueError("S2LSkillAdapterFactory requires request.skill")
        job_id = f"s2l-{uuid.uuid4().hex[:8]}"
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
            backend="s2l",
            job_id=job_id,
        )
        return AdapterJob(
            job_id=job_id,
            backend="s2l",
            status="queued",
            metadata=metadata,
            request=request,
        )

    async def poll(self, job: AdapterJob) -> AdapterJob:
        if job.status in ("done", "failed"):
            return job
        if job.status == "queued":
            job.status = "running"
            job.logs.append("synthesizing S2L training data")
            return job
        if job.status != "running" or job.metadata is None or job.request is None:
            job.status = "failed"
            job.logs.append("invalid job state")
            return job
        try:
            from armature.adapters.backends.continual import resolve_prior_artifact_dir

            skill = job.request.skill
            if skill is None:
                raise ValueError("missing skill")
            dataset = _synthesize_dataset(skill, job.request)
            prior_artifact_dir = resolve_prior_artifact_dir(self._registry, job.request)
            work_dir = Path(tempfile.mkdtemp(prefix="armature-s2l-"))
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
            job.logs.append(f"registered adapter {job.metadata.name}@{job.metadata.version}")
        except Exception as exc:  # pragma: no cover - safety net
            job.status = "failed"
            job.logs.append(f"training failed: {exc}")
        return job


def _synthesize_dataset(skill, request: AdapterRequest) -> TrainingDataset:
    """Build a small SFT dataset from the skill text."""
    body = skill.content or ""
    templates = [
        f"Apply the skill '{skill.description}' to a concrete task.",
        f"Demonstrate '{skill.description}' step by step.",
        f"Using the approach described by '{skill.description}', solve a representative problem.",
        f"Explain how to use '{skill.description}' in practice.",
    ]
    examples: list[TrainingExample] = []
    for i in range(min(8, max(4, len(templates)))):
        prompt = templates[i % len(templates)]
        messages = [
            {
                "role": "system",
                "content": f"You are an expert assistant. Follow the skill described as: {skill.description}",
            },
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": body},
        ]
        examples.append(
            TrainingExample(
                messages=messages,
                skill_id=skill.id,
                source="skill",
            )
        )
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
