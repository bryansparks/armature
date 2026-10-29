"""Redaction of secrets from event text before it leaves the process."""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

# Ported from armature-ops dashboard/lib/redact.js.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[pousr]_\w+"),
    re.compile(r"\bgithub_pat_\w+"),
    re.compile(r"\bxox[abpr]-[\w-]+"),
    re.compile(r"\bsk-ant-[\w-]+"),
    re.compile(r"\bABSK[A-Za-z0-9+/=]+"),
    re.compile(r"\bATATT[\w=-]+"),
    re.compile(r"\blin_api_\w+"),
    re.compile(r"\bAKIA\w{16}"),
    re.compile(r"\b(Authorization)(:?\s*)(?:(?:Bearer|Basic|token)\s+)?\S+", re.IGNORECASE),
    re.compile(r"\b(Bearer)(\s+)\S+", re.IGNORECASE),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]*"),
    re.compile(r"\bark_[\w-]{40,}"),
)

REDACTED = "[redacted]"
_MIN_ENV_VALUE = 4


def _replace(match: re.Match[str]) -> str:
    if match.re.groups >= 2:
        return f"{match.group(1)}{match.group(2)}{REDACTED}"
    return REDACTED


def env_values(names: str | None = None) -> tuple[str, ...]:
    raw = os.environ.get("ARMATURE_REDACT_ENV", "") if names is None else names
    values = []
    for name in raw.split(","):
        value = os.environ.get(name.strip(), "") if name.strip() else ""
        if len(value) >= _MIN_ENV_VALUE:
            values.append(value)
    return tuple(sorted(set(values), key=len, reverse=True))


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(_replace, text)
    return text


def redact_exact(value: Any, secrets: tuple[str, ...]) -> Any:
    """Replace each of `secrets` wherever it appears in `value`, with no patterns."""
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: redact_exact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_exact(v, secrets) for v in value]
    return value


def redact_value(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, secrets) for v in value]
    return value


def default_redactor() -> Callable[[dict[str, Any]], dict[str, Any]]:
    secrets = env_values()
    return lambda payload: redact_value(payload, secrets)
