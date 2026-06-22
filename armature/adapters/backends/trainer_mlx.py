"""MLX LoRA trainer stub for Apple Silicon local training.

If mlx-lora (or the mlx library) is not installed, the trainer reports
unavailable and LocalAdapterFactory falls back to the mock trainer.
"""
from __future__ import annotations

from pathlib import Path

from armature.adapters.data import TrainingDataset
from armature.adapters.factory import AdapterRequest
from armature.adapters.backends.trainer import Trainer


class MLXLoraTrainer(Trainer):
    """Real LoRA trainer using Apple's mlx-lora ecosystem.

    The implementation below is a minimal scaffold. In production it would:
      1. Load the base model using mlx-lm.
      2. Configure LoRA with rank/alpha/target_modules.
      3. Fine-tune on the TrainingDataset examples.
      4. Save adapter weights to work_dir.
    """

    def available(self) -> bool:
        try:
            import mlx  # noqa: F401
            import mlx_lm  # noqa: F401
            return True
        except ImportError:
            return False

    async def train(
        self,
        dataset: TrainingDataset,
        request: AdapterRequest,
        work_dir: Path,
        *,
        prior_artifact_dir: Path | None = None,
    ) -> Path:
        if not self.available():
            raise RuntimeError("MLX trainer is not available; install mlx, mlx-lm")
        self._write_dummy(work_dir, request, prior_artifact_dir=prior_artifact_dir)
        return work_dir

    def _write_dummy(
        self,
        work_dir: Path,
        request: AdapterRequest,
        *,
        prior_artifact_dir: Path | None = None,
    ) -> None:
        import json
        import shutil

        if prior_artifact_dir is not None and prior_artifact_dir.exists():
            shutil.copytree(prior_artifact_dir, work_dir, dirs_exist_ok=True)

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
        if not (work_dir / "adapter.safetensors").exists():
            (work_dir / "adapter.safetensors").write_bytes(b"MLX")
