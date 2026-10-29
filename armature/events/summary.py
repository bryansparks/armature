"""What a run is about: its subject, its trigger reference, and the summary its steps report.

Both the sender and the server use these checks, so a value the CLI accepts is
one the server stores.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

SUBJECT = re.compile(r"^[A-Za-z0-9._/#-]{1,191}$")
TRIGGER_REF_MAX = 4096
TRIGGER_REF_DEPTH = 4
FIELD_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TITLE_MAX = 300
VERDICT_MAX = 64
LABEL_MAX = 100
URL_MAX = 2048
FIELD_MAX = 200
MAX_LINKS = 20
MAX_FIELDS = 30
OUTCOMES = ("pr_opened", "pushed", "recommended", "approved", "blocked", "noop")


def subject_problem(value: Any) -> str | None:
    if not isinstance(value, str) or not SUBJECT.fullmatch(value):
        return "a subject is 1 to 191 letters, digits, '.', '_', '/', '#', or '-'"
    return None


def trigger_ref_problem(value: Any) -> str | None:
    """Why ``value`` is not a trigger reference, or None."""
    if not isinstance(value, dict):
        return "trigger_ref must be a JSON object"
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        for key, child in item.items():
            if not isinstance(key, str):
                return "trigger_ref keys must be strings"
            if isinstance(child, dict):
                if depth >= TRIGGER_REF_DEPTH:
                    return f"trigger_ref nests at most {TRIGGER_REF_DEPTH} objects deep"
                stack.append((child, depth + 1))
            elif not isinstance(child, (str, int, float, bool)):
                return "trigger_ref values must be strings, numbers, booleans, or objects"
    try:
        size = len(json.dumps(value, allow_nan=False).encode("utf-8"))
    except ValueError:
        return "trigger_ref numbers must be finite"
    if size > TRIGGER_REF_MAX:
        return f"trigger_ref is at most {TRIGGER_REF_MAX} bytes"
    return None


def _line(value: Any, limit: int) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return text[:limit] or None


def link_url(value: Any) -> str | None:
    """``value`` when it is an https URL with a host or a same-origin path, else None."""
    if not isinstance(value, str) or len(value) > URL_MAX or any(not ch.isprintable() or ch.isspace() for ch in value):
        return None
    if "\\" in value:
        return None
    if value.startswith("/"):
        return value if not value.startswith("//") else None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme == "https" and parts.hostname else None


def summary_patch(payload: dict[str, Any]) -> dict[str, Any]:
    """The usable part of a ``summary`` event: title, verdict, outcome, links, and fields. Anything else, risk
    included, is ignored, because the server decides a run's risk."""
    patch: dict[str, Any] = {}
    for name, limit in (("title", TITLE_MAX), ("verdict", VERDICT_MAX)):
        value = _line(payload.get(name), limit)
        if value:
            patch[name] = value
    if payload.get("outcome") in OUTCOMES:
        patch["outcome"] = payload["outcome"]
    links = []
    for link in payload.get("links") if isinstance(payload.get("links"), list) else []:
        if not isinstance(link, dict):
            continue
        url, label = link_url(link.get("url")), _line(link.get("label"), LABEL_MAX)
        if url and label:
            links.append({"label": label, "url": url})
    if links:
        patch["links"] = links[:MAX_LINKS]
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    kept = {k: _line(v, FIELD_MAX) for k, v in fields.items() if isinstance(k, str) and FIELD_KEY.fullmatch(k)}
    kept = {k: v for k, v in kept.items() if v}
    if kept:
        patch["fields"] = dict(list(kept.items())[:MAX_FIELDS])
    return patch


def merge_summary(current: Any, patch: dict[str, Any]) -> dict[str, Any]:
    """``patch`` over ``current``: later values win, and a link's URL appears once, with its latest label."""
    merged = dict(current) if isinstance(current, dict) else {}
    for name in ("title", "verdict", "outcome"):
        if name in patch:
            merged[name] = patch[name]
    if "links" in patch:
        old = merged.get("links") if isinstance(merged.get("links"), list) else []
        by_url: dict[str, str] = {}
        for link in (*old, *patch["links"]):
            if isinstance(link, dict) and isinstance(link.get("url"), str):
                by_url[link["url"]] = link.get("label")
        merged["links"] = [{"label": label, "url": url} for url, label in by_url.items()][-MAX_LINKS:]
    if "fields" in patch:
        old = merged.get("fields") if isinstance(merged.get("fields"), dict) else {}
        fields = {**old, **patch["fields"]}
        merged["fields"] = dict(list(fields.items())[-MAX_FIELDS:])
    return merged
