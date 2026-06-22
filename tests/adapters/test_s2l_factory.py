"""Tests for the Skill-to-LoRA (S2L) adapter factory."""
from __future__ import annotations

import pytest
from armature.adapters.backends.s2l import S2LSkillAdapterFactory
from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry
from armature.spec.models import SkillDef


class CapturingTrainer:
    """Test trainer that records the dataset it receives and writes a dummy artifact."""

    def __init__(self) -> None:
        self.datasets: list[TrainingDataset] = []
        self.calls = 0

    def available(self) -> bool:
        return True

    async def train(
        self, dataset, request, work_dir, *, prior_artifact_dir=None
    ) -> __import__("pathlib").Path:
        self.datasets.append(dataset)
        self.prior_dirs = getattr(self, "prior_dirs", [])
        self.prior_dirs.append(prior_artifact_dir)
        self.calls += 1
        (work_dir / "adapter_config.json").write_text("{}")
        (work_dir / "adapter.safetensors").write_bytes(b"MOCK")
        return work_dir


@pytest.fixture
def factory(tmp_path) -> tuple[S2LSkillAdapterFactory, CapturingTrainer]:
    trainer = CapturingTrainer()
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    return S2LSkillAdapterFactory(registry=reg, trainer=trainer), trainer


async def test_submit_requires_skill(factory):
    f, _ = factory
    with pytest.raises(ValueError, match="skill"):
        await f.submit(AdapterRequest(name="x", base_model="m"))


async def test_submit_creates_queued_job(factory):
    f, _ = factory
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b", skill=skill)
    )
    assert job.backend == "s2l"
    assert job.status == "queued"
    assert job.metadata is not None
    assert job.metadata.name == "tdd-workflow"


async def test_poll_synthesizes_dataset_and_trains(factory):
    f, trainer = factory
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b", skill=skill)
    )
    while job.status != "done":
        job = await f.poll(job)
    assert job.status == "done"
    assert job.artifact_path is not None
    assert trainer.calls == 1
    dataset = trainer.datasets[0]
    assert dataset.name == "tdd-workflow"
    assert dataset.base_model == "qwen/qwen2.5-7b"
    assert len(dataset.examples) >= 1
    assert all(ex.skill_id == "tdd" for ex in dataset.examples)
    assert all(ex.source == "skill" for ex in dataset.examples)


async def test_adapter_is_registered_after_training(factory):
    f, _ = factory
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b", skill=skill)
    )
    while job.status != "done":
        job = await f.poll(job)
    resolved = f._registry.get(job.metadata.name, job.metadata.version)
    assert resolved.metadata.base_model == "qwen/qwen2.5-7b"
    assert resolved.artifact_dir.exists()
