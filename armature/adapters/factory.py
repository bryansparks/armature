"""Core adapter factory interface.

A backend implements :class:`AdapterFactory` to turn training data (skill
documents, exported traces, or merged adapters) into a registered LoRA artifact.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from armature.adapters.manifest import AdapterMetadata

if TYPE_CHECKING:
    from armature.spec.models import SkillDef


@dataclass
class AdapterRequest:
    """Parameters for a single adapter training job."""

    name: str
    base_model: str
    skill: "SkillDef | None" = None
    traces_path: Path | None = None
    rank: int = 16
    alpha: int = 32
    target_modules: list[str] = field(default_factory=lambda: ["q_proj", "v_proj"])
    use_dora: bool = False
    continual_learning: bool = False
    prior_adapter_version: str | None = None
    max_tokens_per_example: int = 32768
    output_max_tokens: int = 4096
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterJob:
    """Handle to an in-flight or completed adapter training job."""

    job_id: str
    backend: str
    status: str  # queued | running | done | failed
    metadata: AdapterMetadata | None = None
    request: AdapterRequest | None = None
    logs: list[str] = field(default_factory=list)
    artifact_path: Path | None = None


class AdapterFactory(ABC):
    """Pluggable backend for producing LoRA adapters from training data."""

    @abstractmethod
    async def submit(self, request: AdapterRequest) -> AdapterJob:
        """Submit a training job and return a handle."""

    @abstractmethod
    async def poll(self, job: AdapterJob) -> AdapterJob:
        """Poll for completion. Should be idempotent."""

    @abstractmethod
    def available(self) -> bool:
        """Return True if this backend can run jobs on this machine."""
