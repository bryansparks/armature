"""The environment a run hands to the processes it starts."""
from __future__ import annotations

import os
from collections.abc import Mapping

from armature.events.sign import ENGINE_FD_ENV

RUNNER_KEY = "ARMATURE_RUNNER_KEY"
_PARENT_ONLY = ("ARMATURE_EVENTS", "ARMATURE_EVENTS_TOKEN", "ARMATURE_AGENT_MODELS", "ARMATURE_TOOL_TOKEN",
                "ARMATURE_GATE_DIR", ENGINE_FD_ENV)


def child_env(
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """`base` (default os.environ) without the runner key, inherited event endpoint, or agent credentials, plus `extra`.

    A run that sends events re-injects its own endpoint through `extra`. The runner key is
    never passed on, even through `extra`.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if k != RUNNER_KEY and k not in _PARENT_ONLY}
    env.update(extra or {})
    env.pop(RUNNER_KEY, None)
    return env
