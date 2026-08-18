"""The ``armature tune`` facade: a closed loop that runs the cheap ``improve``
engine by default and escalates to the expensive ``optimize`` engine only when
improve stalls. Builds on the unified ``ImprovementStore`` (Option 4) and reuses
``loop``'s budget/stop accounting pattern.
"""
from armature.tune.stall import StallVerdict, detect_stall

__all__ = ["StallVerdict", "detect_stall"]