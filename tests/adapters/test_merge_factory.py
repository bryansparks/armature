"""Tests for the merged adapter factory."""
from __future__ import annotations

import pytest
from armature.adapters.backends.merge import MergedAdapterFactory
from armature.adapters.backends.mock import MockAdapterFactory
from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry


@pytest.fixture
def factory(tmp_path) -> tuple[MockAdapterFactory, MergedAdapterFactory]:
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    mock = MockAdapterFactory(registry=reg)
    merge = MergedAdapterFactory(registry=reg)
    return mock, merge


async def _make_adapter(mock: MockAdapterFactory, name: str) -> str:
    job = await mock.submit(AdapterRequest(name=name, base_model="qwen/qwen2.5-7b"))
    while job.status != "done":
        job = await mock.poll(job)
    return f"{job.metadata.name}@{job.metadata.version}"


async def test_merge_requires_adapter_refs(factory):
    _, merge = factory
    with pytest.raises(ValueError, match="adapter_refs"):
        await merge.submit(AdapterRequest(name="combo", base_model="m"))


async def test_merge_resolves_and_registers_artifact(factory):
    mock, merge = factory
    ref_a = await _make_adapter(mock, "skill-a")
    ref_b = await _make_adapter(mock, "skill-b")

    job = await merge.submit(
        AdapterRequest(
            name="combo",
            base_model="qwen/qwen2.5-7b",
            extra={"adapter_refs": [ref_a, ref_b]},
        )
    )
    while job.status != "done":
        job = await merge.poll(job)

    assert job.status == "done"
    assert job.artifact_path is not None
    resolved = merge._registry.get(job.metadata.name, job.metadata.version)
    config_text = (resolved.artifact_dir / "adapter_config.json").read_text()
    assert "skill-a" in config_text
    assert "skill-b" in config_text


async def test_merge_bad_reference_format(factory):
    _, merge = factory
    with pytest.raises(ValueError, match="name@version"):
        merge._resolve_refs(["bad-format"])


async def test_merge_unknown_reference(factory):
    _, merge = factory
    with pytest.raises(ValueError):
        merge._resolve_refs(["missing@1"])
