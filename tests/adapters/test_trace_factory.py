"""Tests for the trace-based adapter factory."""
from __future__ import annotations

import json

import pytest
from armature.adapters.backends.trace import TraceAdapterFactory
from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry


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
        (work_dir / "adapter.safetensors").write_bytes(b"MOCK")
        return work_dir


@pytest.fixture
def factory(tmp_path) -> tuple[TraceAdapterFactory, CapturingTrainer]:
    trainer = CapturingTrainer()
    reg = AdapterRegistry(base_dir=tmp_path / "adapters")
    return TraceAdapterFactory(registry=reg, trainer=trainer), trainer


def _write_chat_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


async def test_submit_requires_traces_path(factory):
    f, _ = factory
    with pytest.raises(ValueError, match="traces_path"):
        await f.submit(AdapterRequest(name="x", base_model="m"))


async def test_poll_loads_chat_traces(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                "metadata": {"role_type": "worker", "stage_id": "s1", "score": 0.9},
            },
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "bye"},
                    {"role": "assistant", "content": "goodbye"},
                ],
                "metadata": {"role_type": "judge", "stage_id": "s2", "score": 0.5},
            },
        ],
    )
    job = await f.submit(
        AdapterRequest(name="worker-lora", base_model="m", traces_path=traces)
    )
    while job.status != "done":
        job = await f.poll(job)
    assert job.status == "done"
    assert trainer.calls == 1
    dataset = trainer.datasets[0]
    assert len(dataset.examples) == 2


async def test_role_type_filter(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [{"role": "assistant", "content": "a"}],
                "metadata": {"role_type": "worker"},
            },
            {
                "messages": [{"role": "assistant", "content": "b"}],
                "metadata": {"role_type": "judge"},
            },
        ],
    )
    job = await f.submit(
        AdapterRequest(
            name="judge-lora",
            base_model="m",
            traces_path=traces,
            extra={"role_type": "judge"},
        )
    )
    while job.status != "done":
        job = await f.poll(job)
    dataset = trainer.datasets[0]
    assert len(dataset.examples) == 1


async def test_dpo_format(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    with traces.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"prompt": "p", "chosen": "good", "rejected": "bad", "score": 0.9}
            )
            + "\n"
        )
    job = await f.submit(
        AdapterRequest(name="dpo-lora", base_model="m", traces_path=traces)
    )
    while job.status != "done":
        job = await f.poll(job)
    dataset = trainer.datasets[0]
    assert len(dataset.examples) == 1
    assert dataset.examples[0].messages[-1]["content"] == "good"


async def test_poll_continual_learning_passes_prior_artifact(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                "metadata": {"role_type": "worker", "stage_id": "s1", "score": 0.9},
            }
        ],
    )
    job = await f.submit(
        AdapterRequest(name="worker-lora", base_model="m", traces_path=traces)
    )
    while job.status != "done":
        job = await f.poll(job)
    v1_dir = f._registry.get("worker-lora", "1").artifact_dir

    job2 = await f.submit(
        AdapterRequest(
            name="worker-lora",
            base_model="m",
            traces_path=traces,
            continual_learning=True,
            prior_adapter_version="1",
        )
    )
    while job2.status != "done":
        job2 = await f.poll(job2)
    assert job2.status == "done"
    assert trainer.prior_dirs[-1] == v1_dir


async def test_poll_continual_learning_auto_resolves_latest(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                "metadata": {"role_type": "worker", "stage_id": "s1", "score": 0.9},
            }
        ],
    )
    job = await f.submit(
        AdapterRequest(name="worker-lora", base_model="m", traces_path=traces)
    )
    while job.status != "done":
        job = await f.poll(job)
    v1_dir = f._registry.get("worker-lora", "1").artifact_dir

    job2 = await f.submit(
        AdapterRequest(
            name="worker-lora",
            base_model="m",
            traces_path=traces,
            continual_learning=True,
        )
    )
    while job2.status != "done":
        job2 = await f.poll(job2)
    assert job2.status == "done"
    assert trainer.prior_dirs[-1] == v1_dir


async def test_poll_continual_learning_incompatible_prior(factory, tmp_path):
    f, trainer = factory
    from armature.adapters.manifest import AdapterMetadata

    prior_dir = tmp_path / "prior"
    prior_dir.mkdir()
    (prior_dir / "adapter_config.json").write_text("{}")
    (prior_dir / "adapter.safetensors").write_bytes(b"MOCK")
    f._registry.register(
        AdapterMetadata(
            name="worker-lora",
            version="1",
            base_model="different-model",
            rank=4,
            alpha=8,
            target_modules=["q_proj"],
            use_dora=False,
            backend="mock",
            job_id="prior",
        ),
        prior_dir,
    )

    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                "metadata": {"role_type": "worker", "stage_id": "s1", "score": 0.9},
            }
        ],
    )
    job = await f.submit(
        AdapterRequest(
            name="worker-lora",
            base_model="m",
            traces_path=traces,
            continual_learning=True,
            prior_adapter_version="1",
        )
    )
    while job.status != "failed":
        job = await f.poll(job)
    assert job.status == "failed"
    assert "incompatible" in "\n".join(job.logs).lower()


async def test_preprocess_deduplicates_and_limits(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                "metadata": {"role_type": "worker", "score": 0.9},
            },
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                "metadata": {"role_type": "worker", "score": 0.9},
            },
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "bye"},
                    {"role": "assistant", "content": "goodbye"},
                ],
                "metadata": {"role_type": "worker", "score": 0.5},
            },
        ],
    )
    job = await f.submit(
        AdapterRequest(
            name="worker-lora",
            base_model="m",
            traces_path=traces,
            extra={
                "preprocess": {
                    "min_score": 0.6,
                    "max_examples": 10,
                    "deduplicate": True,
                }
            },
        )
    )
    while job.status != "done":
        job = await f.poll(job)
    dataset = trainer.datasets[-1]
    assert len(dataset.examples) == 1


async def test_preprocess_filters_by_length(factory, tmp_path):
    f, trainer = factory
    traces = tmp_path / "traces.jsonl"
    _write_chat_jsonl(
        traces,
        [
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "short"},
                    {"role": "assistant", "content": "ok"},
                ],
                "metadata": {"score": 0.9},
            },
            {
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "a" * 1000},
                    {"role": "assistant", "content": "b" * 1000},
                ],
                "metadata": {"score": 0.9},
            },
        ],
    )
    job = await f.submit(
        AdapterRequest(
            name="worker-lora",
            base_model="m",
            traces_path=traces,
            extra={"preprocess": {"max_total_length": 100}},
        )
    )
    while job.status != "done":
        job = await f.poll(job)
    dataset = trainer.datasets[-1]
    assert len(dataset.examples) == 1
    assert dataset.examples[0].messages[1]["content"] == "short"
