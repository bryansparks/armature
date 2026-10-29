import importlib

__version__ = "0.7.0"
__all__ = ["Harness", "HarnessSpec"]


def __getattr__(name: str):
    # Lazy, so `python -m armature.events` starts without litellm or the engine.
    if name == "Harness":
        from armature.runtime.engine import Harness
        return Harness
    if name == "HarnessSpec":
        from armature.spec.models import HarnessSpec
        return HarnessSpec
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
