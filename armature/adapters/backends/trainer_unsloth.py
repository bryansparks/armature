"""Unsloth LoRA trainer stub.

If unsloth is not installed, the trainer reports unavailable and
LocalAdapterFactory falls back to the mock trainer.
"""
from __future__ import annotations

from pathlib import Path

from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterRequest
from armature.adapters.backends.trainer import Trainer


class UnslothLoraTrainer(Trainer):
    """Real LoRA trainer using the unsloth fast fine-tuning library.

    The implementation below is a minimal scaffold. In production it would:
      1. Load the base model via unsloth.FastLanguageModel.
      2. Add LoRA adapters with rank/alpha/target_modules.
      3. Fine-tune on the TrainingDataset examples.
      4. Save adapter weights to work_dir.
    """

    def available(self) -> bool:
        try:
            import unsloth  # noqa: F401
            return True
        except ImportError:
            return False

    async def train(
        self,
        dataset: TrainingDataset,
        request: AdapterRequest,
        work_dir: Path,
    ) -> Path:
        if not self.available():
            raise RuntimeError("Unsloth trainer is not available; install unsloth")
        self._write_dummy(work_dir, request)
        return work_dir

    def _write_dummy(self, work_dir: Path, request: AdapterRequest) -> None:
        import json

        config = {
            "lora_alpha": request.alpha,
            "r": request.rank,
            "target_modules": request.target_modules,
            "base_model_name_or_path": request.base_model,
            "use_dora": request.use_dora,
            "continual_learning": request.continual_learning,
            "prior_adapter_version": request.prior_adapter_version,
        }
        (work_dir / "adapter_config.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )
        (work_dir / "adapter.safetensors").write_bytes(b"UNSLTH")
