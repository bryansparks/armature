"""Adapter factory subsystem for Armature.

Provides versioned local storage of LoRA adapter artifacts and metadata
used by the spec/runtime integration.
"""

from armature.adapters.registry import AdapterMetadata, AdapterRegistry

__all__ = ["AdapterMetadata", "AdapterRegistry"]
