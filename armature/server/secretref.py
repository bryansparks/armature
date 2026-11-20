"""Resolve a secret reference such as ``env:NAME`` or ``file:/path`` to its value when it is needed.

Any other scheme goes to the configured secret provider, a ``module:callable`` that takes the whole
reference and returns the value, so a deployment package can supply KMS or a secrets manager without
the core importing a cloud SDK. The server calls the provider from several threads at once, so it must be
thread-safe. Errors name the reference, never the value.
"""
from __future__ import annotations

import importlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

BUILTIN_SCHEMES = ("env", "file")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")
PROVIDER = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class SecretRefError(Exception):
    """A secret reference that can't be resolved, or a secret stored unsafely."""


def scheme_of(ref: str) -> str:
    scheme, _, rest = ref.partition(":")
    if not SCHEME.match(scheme) or not rest:
        raise SecretRefError(f"secret reference must be <scheme>:<name>, got {ref!r}")
    return scheme


def resolve(ref: str, *, provider: str | None = None, env: Mapping[str, str] | None = None,
            label: str = "secret") -> str:
    scheme = scheme_of(ref)
    rest = ref.partition(":")[2]
    if scheme == "env":
        env = os.environ if env is None else env
        value = env.get(rest)
        if not value:
            raise SecretRefError(f"{label} variable {rest} is not set")
        return value
    if scheme == "file":
        return read_private_file(Path(rest).expanduser(), label).rstrip("\r\n")
    if provider is None:
        raise SecretRefError(f"no secret provider is configured for {scheme}: references; set secret_provider")
    fn = load_provider(provider)
    try:
        value = fn(ref)
    except Exception as exc:  # noqa: BLE001
        raise SecretRefError(f"secret provider {provider} failed for a {scheme}: reference "
                             f"({exc.__class__.__name__})") from None
    if not isinstance(value, str) or not value:
        raise SecretRefError(f"secret provider {provider} returned no value for a {scheme}: reference")
    return value


def load_provider(provider: str) -> Callable[[str], str]:
    if not PROVIDER.match(provider):
        raise SecretRefError(f"secret_provider must be module:callable, got {provider!r}")
    module_name, _, attr = provider.partition(":")
    try:
        fn = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise SecretRefError(f"secret provider {provider} can't be loaded: {exc}") from exc
    if not callable(fn):
        raise SecretRefError(f"secret provider {provider} is not callable")
    return fn


def read_private_file(path: Path, label: str = "secret") -> str:
    """Read a regular file only this user can read, refusing a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise SecretRefError(f"{label} file {path} does not exist") from exc
    except OSError as exc:
        raise SecretRefError(f"{label} file {path} can't be opened: {exc.strerror}") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretRefError(f"{label} file {path} is not a regular file")
        if info.st_mode & 0o077:
            raise SecretRefError(f"{label} file {path} has mode {stat.S_IMODE(info.st_mode):04o}; chmod 600 it")
        if info.st_uid != os.getuid():
            raise SecretRefError(f"{label} file {path} is not owned by this user")
        return handle.read()
