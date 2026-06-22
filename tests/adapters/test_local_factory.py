"""Tests for the local adapter factory."""
from __future__ import annotations

import pytest
from armature.adapters.backends.local import LocalAdapterFactory
from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry
from armature.spec.models import SkillDef


class CapturingTrainer:
    def __init__(self) -> None:
        self.datasets: list[TrainingDataset] = []
        self.calls = 0

    def available(self) -> bool:
        return True

    async def train(self, dataset, request, work_dir, *, prior_artifact_dir=None):
        self.datasets.append(dataset)
        self.prior_dirs = getattr(self, "prior_dirs", [])
        self.prior_dirs.append(prior_artifact_dir)
        self.calls += 1
        (work_dir / "adapter_config.json").write_text("{}")
        (work_dir / "adapter.safetensors").write_bytes(b"LOCAL")
        return work_dir


@pytest.fixture
def factory(tmp_path) -> tuple[LocalAdapterFactory, CapturingTrainer]:
    trainer = CapturingTrainer()
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    return LocalAdapterFactory(registry=reg, trainer=trainer), trainer


async def test_submit_creates_queued_job(factory):
    f, _ = factory
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(
        AdapterRequest(name="tdd", base_model="m", skill=skill)
    )
    assert job.backend == "local"
    assert job.status == "queued"


async def test_poll_trains_from_skill(factory):
    f, trainer = factory
    skill = SkillDef(id="tdd", description="TDD", content="Write tests first.")
    job = await f.submit(
        AdapterRequest(name="tdd", base_model="m", skill=skill)
    )
    while job.status != "done":
        job = await f.poll(job)
    assert job.status == "done"
    assert trainer.calls == 1
    assert len(trainer.datasets[0].examples) >= 1


async def test_unavailable_with_explicitly_disabled_trainer(tmp_path):
    # When no trainer is supplied and discovery cannot find one, the factory is
    # unavailable. Discovery may find torch/peft in this environment, so pass a
    # fake trainer that reports unavailable.
    class DisabledTrainer:
        def available(self) -> bool:
            return False

    f = LocalAdapterFactory(
        registry=AdapterRegistry(base_dir=tmp_path / "adapters"),
        trainer=DisabledTrainer(),
    )
    assert not f.available()
    job = await f.submit(AdapterRequest(name="x", base_model="m"))
    job = await f.poll(job)
    assert job.status == "failed"
