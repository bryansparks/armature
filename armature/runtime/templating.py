"""Sandboxed Jinja2 environments: the only way Armature renders a template.

Every template a spec carries (skip_if, condition, partition_source, loop
until, gate messages, tool_call args, role descriptions, adapter cmd, and the
spec file itself at load time) renders through one of these, so attribute
access such as ``''.__class__`` and calls to mutating methods raise
``jinja2.sandbox.SecurityError`` instead of reaching Python internals.
"""
from __future__ import annotations

from functools import lru_cache

from jinja2 import BaseLoader, ChainableUndefined, Undefined
from jinja2.nativetypes import NativeEnvironment
from jinja2.sandbox import ImmutableSandboxedEnvironment


class SandboxedNativeEnvironment(NativeEnvironment, ImmutableSandboxedEnvironment):
    """Returns native Python values like NativeEnvironment, with the immutable sandbox's checks."""


@lru_cache(maxsize=None)
def text_env(undefined: type[Undefined] = Undefined) -> ImmutableSandboxedEnvironment:
    """A sandboxed environment that renders to a string."""
    return ImmutableSandboxedEnvironment(loader=BaseLoader(), undefined=undefined)


@lru_cache(maxsize=None)
def native_env(undefined: type[Undefined] = ChainableUndefined) -> SandboxedNativeEnvironment:
    """A sandboxed environment that renders a lone expression to its Python value."""
    return SandboxedNativeEnvironment(undefined=undefined)
