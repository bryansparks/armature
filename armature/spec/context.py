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

WORK_UNIT_LAYER_NAME = "work_unit"               # synthesized from the injected record
WORK_UNIT_LAYER_PRECEDENCE = MISSION_LAYER_PRECEDENCE - 1  # renders after the mission


@dataclass(frozen=True)
class EffectiveContextPolicy:
    must: tuple[str, ...]
    never: frozenset[str]

    def as_dict(self) -> dict:
        return {"must": list(self.must), "never": sorted(self.never)}


def mission_layer(
    spec: HarnessSpec,
    work_record: dict | None = None,
) -> ContextLayer | None:
    """The auto layer synthesized from spec.mission, or None if not applicable.

    Built on the fly — the loaded spec is never mutated, so spec_version
    stays a faithful hash of what the author wrote.

    With a work record and `mission_source: work_unit` (design §4), the
    record's mission_objective replaces spec.mission — the mission document,
    not the spec, is the origin of the objective when driven by a work unit.
    """
    content = spec.mission
    if work_record is not None and spec.mission_source == "work_unit":
        content = (work_record.get("mission_objective") or "").strip() or spec.mission
    if not content:
        return None
    if any(l.name == MISSION_LAYER_NAME for l in spec.context_layers):
        return None  # reserved name is a validation error; nothing to synthesize
    return ContextLayer(
        name=MISSION_LAYER_NAME,
        precedence=MISSION_LAYER_PRECEDENCE,
        content=content,
    )


def work_unit_layer(spec: HarnessSpec, work_record: dict | None) -> ContextLayer | None:
    """The auto layer synthesized from the injected work record (design §4).

    Only when the spec opts in via `mission_source: work_unit` AND the
    executor injected a `work_unit` record — an ordinary `armature run` of an
    opted-in spec (no record) falls back to static mission behavior.
    Carries the unit's title + objective, plus requires awareness so the
    agent can notice it is one slice of a larger mission.
    """
    if spec.mission_source != "work_unit" or not work_record:
        return None
    if any(l.name == WORK_UNIT_LAYER_NAME for l in spec.context_layers):
        return None  # reserved name; nothing to synthesize
    title = (work_record.get("title") or "").strip()
    objective = (work_record.get("objective") or "").strip()
    if not title and not objective:
        return None
    content = "\n".join(part for part in (title, objective) if part)
    requires = work_record.get("requires") or []
    if requires:
        content += (f"\nThis unit is one slice of the mission; "
                    f"it waited on: {', '.join(requires)}.")
    return ContextLayer(
        name=WORK_UNIT_LAYER_NAME,
        precedence=WORK_UNIT_LAYER_PRECEDENCE,
        content=content,
    )


def ordered_layers(
    spec: HarnessSpec,
    work_record: dict | None = None,
) -> list[ContextLayer]:
    """All layers (mission + work_unit pseudo-layers included), highest
    precedence first.

    Python's sort is stable, so equal precedences keep declaration order.
    """
    layers = list(spec.context_layers)
    m = mission_layer(spec, work_record)
    if m is not None:
        layers.append(m)
    w = work_unit_layer(spec, work_record)
    if w is not None:
        layers.append(w)
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
    if spec.mission_source == "work_unit":
        keys.add("work_unit")          # record dict injected by the executor
    return frozenset(keys)


def resolve_effective_policy(
    spec: HarnessSpec,
    stage: Stage,
    work_record: dict | None = None,
) -> EffectiveContextPolicy:
    """The §5.3 formula. must is subtracted by never — never re-added."""
    never: set[str] = set(floor_never(spec))
    must: set[str] = set()
    if spec.context_policy is not None:
        never.update(spec.context_policy.never)
        must.update(spec.context_policy.must)
    if stage.context_policy is not None:
        never.update(stage.context_policy.never)
        must.update(stage.context_policy.must)
    if mission_layer(spec, work_record) is not None:
        must.add(MISSION_LAYER_NAME)
    if work_unit_layer(spec, work_record) is not None:
        must.add(WORK_UNIT_LAYER_NAME)
    must -= never
    return EffectiveContextPolicy(must=tuple(sorted(must)), never=frozenset(never))
