"""Promotion policies for adapter registry updates.

The registry can automatically advance the ``latest`` pointer when a new
adapter version is registered, but that should only happen when the new version
meets quality criteria. Policies encapsulate those criteria.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from armature.adapters.manifest import AdapterMetadata


class PromotionPolicy(ABC):
    """Decide whether a newly registered adapter version should become latest."""

    @abstractmethod
    def should_promote(
        self,
        new: AdapterMetadata,
        current: AdapterMetadata | None,
    ) -> bool:
        """Return True if ``new`` should replace ``current`` as the latest version."""


class AlwaysPromotePolicy(PromotionPolicy):
    """Always advance the latest pointer. This is the historical default."""

    def should_promote(
        self,
        new: AdapterMetadata,
        current: AdapterMetadata | None,
    ) -> bool:
        return True


class NeverPromotePolicy(PromotionPolicy):
    """Never advance the latest pointer automatically."""

    def should_promote(
        self,
        new: AdapterMetadata,
        current: AdapterMetadata | None,
    ) -> bool:
        return False


@dataclass
class ThresholdPromotionPolicy(PromotionPolicy):
    """Promote only when the new version meets a validation-score threshold."""

    min_score: float = 0.0

    def should_promote(
        self,
        new: AdapterMetadata,
        current: AdapterMetadata | None,
    ) -> bool:
        if new.validation_score is None:
            return False
        return new.validation_score >= self.min_score


@dataclass
class CompositePromotionPolicy(PromotionPolicy):
    """Promote only when every wrapped policy agrees."""

    policies: list[PromotionPolicy]

    def should_promote(
        self,
        new: AdapterMetadata,
        current: AdapterMetadata | None,
    ) -> bool:
        return all(p.should_promote(new, current) for p in self.policies)
