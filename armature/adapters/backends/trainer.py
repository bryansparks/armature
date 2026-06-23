"""Trainer abstraction used by adapter backends.

A trainer turns a :class:`armature.adapters.data.TrainingDataset` into a directory
containing LoRA adapter weights. Concrete implementations may use PEFT,
mlx-lora, unsloth, or any other fine-tuning library. The default mock trainer
produces tiny placeholder artifacts for tests.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterRequest
from armature.adapters.manifest import AdapterMetadata


class Trainer(ABC):
    """Pluggable training step for adapter factories."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if the required ML libraries are installed."""

    @abstractmethod
    async def train(
        self,
        dataset: TrainingDataset,
        request: AdapterRequest,
        work_dir: Path,
    ) -> Path:
        """Train an adapter and return the artifact directory."""


class MockTrainer(Trainer):
    """Trainer that writes a deterministic, minimal LoRA-shaped artifact."""

    def available(self) -> bool:
        return True

    async def train(
        self,
        dataset: TrainingDataset,
        request: AdapterRequest,
        work_dir: Path,
    ) -> Path:
        metadata = AdapterMetadata(
            name=request.name,
            version="1",
            base_model=request.base_model,
            rank=request.rank,
            alpha=request.alpha,
            target_modules=list(request.target_modules),
        )
        return _write_dummy_artifact(metadata, work_dir)


def _write_dummy_artifact(metadata: AdapterMetadata, work_dir: Path) -> Path:
    config = {
        "lora_alpha": metadata.alpha,
        "r": metadata.rank,
        "target_modules": metadata.target_modules,
        "base_model_name_or_path": metadata.base_model,
        "use_dora": metadata.use_dora,
        "continual_learning": metadata.continual_learning,
        "prior_adapter_version": metadata.prior_adapter_version,
    }
    (work_dir / "adapter_config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    (work_dir / "adapter.safetensors").write_bytes(b"MOCK")
    return work_dir
