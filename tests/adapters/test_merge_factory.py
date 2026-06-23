"""Tests for the merged adapter factory."""
from __future__ import annotations

from pathlib import Path

import pytest
from armature.adapters.backends.merge import MergedAdapterFactory
from armature.adapters.backends.mock import MockAdapterFactory
from armature.adapters.factory import AdapterRequest
from armature.adapters.manifest import AdapterMetadata
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


def _write_real_adapter(
    registry: AdapterRegistry,
    tmp_path: Path,
    name: str,
    base_model: str = "qwen/qwen2.5-7b",
    rank: int = 4,
    alpha: int = 8,
    target_modules: list[str] | None = None,
    use_dora: bool = False,
    weight_value: float = 0.1,
) -> str:
    """Create a real safetensors LoRA artifact and register it."""
    torch = pytest.importorskip("torch")
    from safetensors.torch import save_file

    target_modules = target_modules or ["q_proj"]
    artifact_dir = tmp_path / "real" / name
    artifact_dir.mkdir(parents=True)
    metadata = AdapterMetadata(
        name=name,
        version="1",
        base_model=base_model,
        rank=rank,
        alpha=alpha,
        target_modules=list(target_modules),
        use_dora=use_dora,
        backend="mock",
        job_id="real-1",
    )
    state: dict[str, torch.Tensor] = {}
    for module in target_modules:
        base = f"base_model.model.{module}"
        state[f"{base}.lora_A.default.weight"] = torch.full(
            (rank, 16), weight_value, dtype=torch.float32
        )
        state[f"{base}.lora_B.default.weight"] = torch.full(
            (32, rank), weight_value, dtype=torch.float32
        )
        if use_dora:
            state[f"{base}.lora_magnitude_vector.default.weight"] = torch.full(
                (32,), weight_value, dtype=torch.float32
            )
    save_file(state, artifact_dir / "adapter.safetensors")
    registry.register(metadata, artifact_dir)
    return f"{name}@1"


async def test_merge_adds_lora_weights(factory, tmp_path):
    _, merge = factory
    ref_a = _write_real_adapter(
        merge._registry, tmp_path, "skill-a", weight_value=0.1
    )
    ref_b = _write_real_adapter(
        merge._registry, tmp_path, "skill-b", weight_value=0.2
    )

    job = await merge.submit(
        AdapterRequest(
            name="combo",
            base_model="qwen/qwen2.5-7b",
            rank=4,
            alpha=8,
            target_modules=["q_proj"],
            extra={"adapter_refs": [ref_a, ref_b]},
        )
    )
    while job.status != "done":
        job = await merge.poll(job)

    assert job.status == "done"
    from safetensors.torch import load_file

    merged_state = load_file(job.artifact_path / "adapter.safetensors")
    assert merged_state[
        "base_model.model.q_proj.lora_A.default.weight"
    ][0, 0].item() == pytest.approx(0.3)
    assert merged_state[
        "base_model.model.q_proj.lora_B.default.weight"
    ][0, 0].item() == pytest.approx(0.3)


async def test_merge_weighted_merge(factory, tmp_path):
    _, merge = factory
    ref_a = _write_real_adapter(
        merge._registry, tmp_path, "skill-a", weight_value=0.1
    )
    ref_b = _write_real_adapter(
        merge._registry, tmp_path, "skill-b", weight_value=0.2
    )

    job = await merge.submit(
        AdapterRequest(
            name="combo-weighted",
            base_model="qwen/qwen2.5-7b",
            rank=4,
            alpha=8,
            target_modules=["q_proj"],
            extra={"adapter_refs": [ref_a, ref_b], "merge_weights": [2.0, 0.5]},
        )
    )
    while job.status != "done":
        job = await merge.poll(job)

    assert job.status == "done"
    from safetensors.torch import load_file

    merged_state = load_file(job.artifact_path / "adapter.safetensors")
    assert merged_state[
        "base_model.model.q_proj.lora_A.default.weight"
    ][0, 0].item() == pytest.approx(0.2 + 0.1)


async def test_merge_dora_merges_magnitude(factory, tmp_path):
    _, merge = factory
    ref_a = _write_real_adapter(
        merge._registry, tmp_path, "skill-a", use_dora=True, weight_value=0.1
    )
    ref_b = _write_real_adapter(
        merge._registry, tmp_path, "skill-b", use_dora=True, weight_value=0.2
    )

    job = await merge.submit(
        AdapterRequest(
            name="combo-dora",
            base_model="qwen/qwen2.5-7b",
            rank=4,
            alpha=8,
            target_modules=["q_proj"],
            use_dora=True,
            extra={"adapter_refs": [ref_a, ref_b]},
        )
    )
    while job.status != "done":
        job = await merge.poll(job)

    assert job.status == "done"
    from safetensors.torch import load_file

    merged_state = load_file(job.artifact_path / "adapter.safetensors")
    assert "base_model.model.q_proj.lora_magnitude_vector.default.weight" in merged_state
    assert merged_state[
        "base_model.model.q_proj.lora_magnitude_vector.default.weight"
    ][0].item() == pytest.approx(0.3)


async def test_merge_incompatible_base_model(factory, tmp_path):
    _, merge = factory
    ref_a = _write_real_adapter(
        merge._registry, tmp_path, "skill-a", base_model="a"
    )
    ref_b = _write_real_adapter(
        merge._registry, tmp_path, "skill-b", base_model="b"
    )

    job = await merge.submit(
        AdapterRequest(
            name="combo",
            base_model="a",
            rank=4,
            alpha=8,
            target_modules=["q_proj"],
            extra={"adapter_refs": [ref_a, ref_b]},
        )
    )
    while job.status != "failed":
        job = await merge.poll(job)

    assert job.status == "failed"
    assert "base_model" in "\n".join(job.logs)


async def test_merge_incompatible_use_dora(factory, tmp_path):
    _, merge = factory
    ref_a = _write_real_adapter(
        merge._registry, tmp_path, "skill-a", use_dora=False
    )
    ref_b = _write_real_adapter(
        merge._registry, tmp_path, "skill-b", use_dora=True
    )

    job = await merge.submit(
        AdapterRequest(
            name="combo",
            base_model="qwen/qwen2.5-7b",
            rank=4,
            alpha=8,
            target_modules=["q_proj"],
            use_dora=False,
            extra={"adapter_refs": [ref_a, ref_b]},
        )
    )
    while job.status != "failed":
        job = await merge.poll(job)

    assert job.status == "failed"
    assert "use_dora" in "\n".join(job.logs)
