"""Helpers for continual adapter learning.

Continual LoRA updating builds a new adapter version from a prior version plus
new training examples. The canonical reference is the C-LoRA paper
(arXiv:2502.17920v1): keep the prior low-rank decomposition frozen and learn a
near-zero delta with an orthogonality regularizer to reduce catastrophic
forgetting.

This module is intentionally scaffold-level: it resolves the prior artifact and
verifies compatibility, then passes the artifact directory to the trainer. A
production trainer can use the prior weights as a warm start and add the
orthogonality loss.
"""
from __future__ import annotations

from pathlib import Path

from armature.adapters.factory import AdapterRequest
from armature.adapters.registry import AdapterRegistry


def resolve_prior_artifact_dir(
    registry: AdapterRegistry,
    request: AdapterRequest,
) -> Path | None:
    """Return the prior adapter artifact directory for a continual update.

    If ``request.prior_adapter_version`` is set, that exact version is used.
    Otherwise, if ``request.contual_learning`` is enabled and a prior version of
    the same adapter name exists, the latest version is resolved automatically.
    Raises ``ValueError`` if the prior adapter is incompatible with the request.
    """
    prior_version = request.prior_adapter_version
    if not prior_version and request.continual_learning:
        try:
            prior_version = registry.get(request.name).metadata.version
        except ValueError:
            prior_version = None

    if not prior_version:
        return None

    resolved = registry.get(request.name, prior_version)
    _assert_compatible(resolved.metadata, request)
    return resolved.artifact_dir


def _assert_compatible(prior_meta, request: AdapterRequest) -> None:
    """Ensure the prior adapter can serve as a warm start for the request."""
    mismatches: list[str] = []
    if prior_meta.base_model != request.base_model:
        mismatches.append(
            f"base_model {prior_meta.base_model!r} != {request.base_model!r}"
        )
    if prior_meta.rank != request.rank:
        mismatches.append(f"rank {prior_meta.rank} != {request.rank}")
    if prior_meta.alpha != request.alpha:
        mismatches.append(f"alpha {prior_meta.alpha} != {request.alpha}")
    if prior_meta.target_modules != list(request.target_modules):
        mismatches.append(
            f"target_modules {prior_meta.target_modules} != {list(request.target_modules)}"
        )
    if prior_meta.use_dora != request.use_dora:
        mismatches.append(f"use_dora {prior_meta.use_dora} != {request.use_dora}")
    if mismatches:
        raise ValueError(
            f"Prior adapter {request.name}@{prior_meta.version} is incompatible: "
            + ", ".join(mismatches)
        )
