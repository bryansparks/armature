"""Tests for the mock adapter factory."""
from __future__ import annotations

import pytest
from armature.adapters.backends.mock import MockAdapterFactory
from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry


@pytest.fixture
def factory(tmp_path) -> MockAdapterFactory:
    return MockAdapterFactory(registry=AdapterRegistry(base_dir=tmp_path / "adapters"))


async def test_submit_returns_queued_job(factory):
    job = await factory.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b")
    )
    assert job.backend == "mock"
    assert job.status == "queued"
    assert job.metadata is not None
    assert job.metadata.name == "tdd-workflow"
    assert job.metadata.base_model == "qwen/qwen2.5-7b"


async def test_poll_transitions_through_running_to_done(factory):
    job = await factory.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b")
    )
    job = await factory.poll(job)
    assert job.status == "running"
    job = await factory.poll(job)
    assert job.status == "done"
    assert job.artifact_path is not None


async def test_done_job_is_idempotent(factory):
    job = await factory.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b")
    )
    for _ in range(3):
        job = await factory.poll(job)
    assert job.status == "done"


async def test_artifact_is_registered(factory):
    job = await factory.submit(
        AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b")
    )
    while job.status != "done":
        job = await factory.poll(job)
    resolved = factory._registry.get(job.metadata.name, job.metadata.version)
    assert resolved.artifact_dir.exists()
    assert (resolved.artifact_dir / "adapter_config.json").exists()
    assert (resolved.artifact_dir / "adapter.safetensors").exists()


async def test_versions_increment(factory):
    for _ in range(3):
        job = await factory.submit(
            AdapterRequest(name="tdd-workflow", base_model="qwen/qwen2.5-7b")
        )
        while job.status != "done":
            job = await factory.poll(job)
    versions = sorted(
        [meta.version for meta, _ in factory._registry.list("tdd-workflow")]
    )
    assert versions == ["1", "2", "3"]
