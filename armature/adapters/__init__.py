"""Adapter factory subsystem for Armature.

Provides versioned local storage of LoRA adapter artifacts and metadata
used by the spec/runtime integration.
"""

from armature.adapters.data import TrainingDataset, TrainingExample
from armature.adapters.factory import AdapterFactory, AdapterJob, AdapterRequest
from armature.adapters.manifest import AdapterMetadata
from armature.adapters.registry import AdapterRegistry

__all__ = [
    "AdapterFactory",
    "AdapterJob",
    "AdapterMetadata",
    "AdapterRegistry",
    "AdapterRequest",
    "TrainingDataset",
    "TrainingExample",
]
