"""Pure context-governance resolution — no I/O, no spec mutation.

Single source of truth for layer ordering, the mission pseudo-layer, floor
closures, the harness-injected key set, and per-stage effective policies.
The engine, validator, and trace store all call into this module.

Resolution formula (design §5.3) — a closure at any level always wins over
a force; must is subtracted by never, never re-added:

    effective_never = floor_never ∪ default.never ∪ stage.never
    effective_must  = (default.must ∪ stage.must ∪ {mission}) − effective_never
"""
from __future__ import annotations

from dataclasses import dataclass

from armature.spec.models import ContextLayer, HarnessSpec, Stage

MISSION_LAYER_NAME = "mission"
MISSION_LAYER_PRECEDENCE = -1000  # renders last = bottom of the context block


@dataclass(frozen=True)
class EffectiveContextPolicy:
    must: tuple[str, ...]
    never: frozenset[str]

    def as_dict(self) -> dict:
        return {"must": list(self.must), "never": sorted(self.never)}


def mission_layer(spec: HarnessSpec) -> ContextLayer | None:
    """The auto layer synthesized from spec.mission, or None if not applicable.

    Built on the fly — the loaded spec is never mutated, so spec_version
    stays a faithful hash of what the author wrote.
    """
    if not spec.mission:
        return None
    if any(l.name == MISSION_LAYER_NAME for l in spec.context_layers):
        return None  # reserved name is a validation error; nothing to synthesize
    return ContextLayer(
        name=MISSION_LAYER_NAME,
        precedence=MISSION_LAYER_PRECEDENCE,
        content=spec.mission,
    )


def ordered_layers(spec: HarnessSpec) -> list[ContextLayer]:
    """All layers (mission pseudo-layer included), highest precedence first.

    Python's sort is stable, so equal precedences keep declaration order.
    """
    layers = list(spec.context_layers)
    m = mission_layer(spec)
    if m is not None:
        layers.append(m)
    layers.sort(key=lambda l: -l.precedence)
    return layers


def floor_never(spec: HarnessSpec) -> frozenset[str]:
    """Union of every layer's never — unconditional, non-relaxable."""
    keys: set[str] = set()
    for layer in spec.context_layers:
        keys.update(layer.never)
    return frozenset(keys)


def runtime_context_keys(spec: HarnessSpec) -> frozenset[str]:
    """Context keys the harness itself injects — valid `never` targets."""
    keys = {
        "run_id",               # set by Harness.run() before any stage executes
        "_transcript",          # available in post_run stages
        "_diagnostics",         # available in post_run stages
        "_stale_memory_keys",   # injected when memory has stale entries
        "_memory_index",        # injected when navigation_tools is True
    }
    if spec.continuation:
        keys.add(spec.continuation.inject_as)
    if spec.memory:
        keys.add(spec.memory.inject_as)
        if spec.memory.inject_knowledge_as:
            keys.add(spec.memory.inject_knowledge_as)
    return frozenset(keys)


def resolve_effective_policy(spec: HarnessSpec, stage: Stage) -> EffectiveContextPolicy:
    """The §5.3 formula. must is subtracted by never — never re-added."""
    never: set[str] = set(floor_never(spec))
    must: set[str] = set()
    if spec.context_policy is not None:
        never.update(spec.context_policy.never)
        must.update(spec.context_policy.must)
    if stage.context_policy is not None:
        never.update(stage.context_policy.never)
        must.update(stage.context_policy.must)
    if mission_layer(spec) is not None:
        must.add(MISSION_LAYER_NAME)
    must -= never
    return EffectiveContextPolicy(must=tuple(sorted(must)), never=frozenset(never))
