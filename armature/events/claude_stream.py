"""Turns Claude Code `--output-format stream-json --verbose` lines into events."""
from __future__ import annotations

import json
from typing import Any, BinaryIO

from armature.events.emitter import Emitter

SUMMARY_CHARS = 2048
# What a provider may price per alias, in USD per million tokens. A priced alias's cost replaces the reported one.
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


def _text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = "\n".join(
            block.get("text", "") if isinstance(block, dict) and block.get("type") == "text"
            else json.dumps(block, ensure_ascii=False, default=str)
            for block in value
        )
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text


_MODEL_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "costUSD": "cost_usd",
}


def model_usage(value: Any) -> dict[str, dict[str, Any]] | None:
    """The result's ``modelUsage``, per model, with only its token and cost counts."""
    if not isinstance(value, dict):
        return None
    found = {}
    for model, usage in value.items():
        if isinstance(model, str) and isinstance(usage, dict):
            found[model] = {name: usage[key] for key, name in _MODEL_USAGE_FIELDS.items()
                            if isinstance(usage.get(key), int | float) and not isinstance(usage.get(key), bool)}
    return found or None


def priced_cost(prices: dict[str, Any], input_tokens: Any, output_tokens: Any, cache_read: Any = 0,
                cache_write: Any = 0) -> float:
    """The cost of these tokens at a provider's prices for an alias. A price left out counts as zero."""
    tokens = {"input": input_tokens, "output": output_tokens, "cache_read": cache_read, "cache_write": cache_write}
    return sum(float(prices.get(name) or 0) * float(tokens[name] or 0) for name in PRICE_FIELDS) / 1_000_000


def priced_usage(usage: dict[str, dict[str, Any]] | None, prices: dict[str, Any] | None,
                 model: str | None) -> dict[str, dict[str, Any]] | None:
    """``model_usage`` with the priced model's entry at the provider's prices. Other models, such as a subagent's,
    keep the cost Claude Code reports."""
    if usage is None or prices is None:
        return usage
    return {name: {**counts, "cost_usd": priced_cost(prices, counts.get("input_tokens"), counts.get("output_tokens"),
                                                     counts.get("cache_read_input_tokens"),
                                                     counts.get("cache_creation_input_tokens"))}
            if name == model else counts for name, counts in usage.items()}


def session_cost(result: dict[str, Any], prices: dict[str, Any] | None, model: str | None = None) -> float:
    """A session result's cost: the one Claude Code reports, or with ``model``'s tokens at the provider's prices.
    The result's overall usage is priced only when it has no per-model usage."""
    if prices is None:
        return float(result.get("total_cost_usd") or 0.0)
    usage = priced_usage(model_usage(result.get("modelUsage")), prices, model)
    if usage is None:
        totals = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return priced_cost(prices, totals.get("input_tokens"), totals.get("output_tokens"),
                           totals.get("cache_read_input_tokens"), totals.get("cache_creation_input_tokens"))
    return sum(float(counts.get("cost_usd") or 0) for counts in usage.values())


class ClaudeStreamParser:
    def __init__(self, emitter: Emitter, step_id: str | None = None, fields: dict[str, Any] | None = None,
                 cost_fields: dict[str, Any] | None = None, prices: dict[str, Any] | None = None,
                 priced_model: str | None = None):
        self._emitter = emitter
        # The provider's prices for the model the session runs, which replace the cost reported for that model.
        self._prices = prices
        self._priced_model = priced_model
        self._step_id = step_id
        self._fields = dict(fields or {})
        # What the step asked for, such as its effort and provider, which only the cost event carries.
        self._cost_fields = dict(cost_fields or {})
        self._tool_names: dict[str, str] = {}
        self._started = False
        self._ended = False
        self._model: str | None = None
        self._effort: str | None = None
        self._session_id: str | None = None

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        handler = getattr(self, f"_on_{message.get('type')}", None)
        if handler is not None:
            handler(message)

    def finish(self) -> None:
        if self._started and not self._ended:
            self._emit("session_end", status="incomplete", session_id=self._session_id)
            self._ended = True

    def _emit(self, kind: str, **payload: Any) -> None:
        self._emitter.emit(kind, self._step_id, **{**self._fields, **payload})

    def _summary(self, value: Any) -> str:
        # Redact before cutting, so a cut can't leave part of a secret behind.
        return self._emitter.redact({"text": _text(value)}).get("text", "")[:SUMMARY_CHARS]

    def _on_system(self, message: dict[str, Any]) -> None:
        if message.get("subtype") != "init" or self._started:
            return
        self._started = True
        self._model = message.get("model")
        effort = message.get("effort")
        self._effort = effort if isinstance(effort, str) else None
        self._session_id = message.get("session_id")
        self._emit("session_start", model=self._model, session_id=self._session_id,
                   **({"effort": self._effort} if self._effort else {}))

    def _on_assistant(self, message: dict[str, Any]) -> None:
        body = message.get("message") or {}
        for block in body.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_id = block.get("id") or ""
                name = block.get("name") or ""
                self._tool_names[tool_id] = name
                self._emit("tool_call", tool=name, tool_use_id=tool_id, input=self._summary(block.get("input")))
            elif block.get("type") == "text" and block.get("text"):
                self._emit("text", text=block["text"])

    def _on_user(self, message: dict[str, Any]) -> None:
        body = message.get("message") or {}
        content = body.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id") or ""
            self._emit(
                "tool_result",
                tool=self._tool_names.get(tool_id, ""),
                tool_use_id=tool_id,
                is_error=bool(block.get("is_error")),
                content=self._summary(block.get("content", "")),
            )

    def _on_result(self, message: dict[str, Any]) -> None:
        usage = message.get("usage") or {}
        attribution = {**self._cost_fields, "effort": self._effort,
                       "model_usage": priced_usage(model_usage(message.get("modelUsage")), self._prices,
                                                   self._priced_model)}
        self._emit(
            "cost",
            model=self._model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=session_cost(message, self._prices, self._priced_model),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            session_id=message.get("session_id") or self._session_id,
            **{key: value for key, value in attribution.items() if value is not None},
        )
        self._emit(
            "session_end",
            status=message.get("subtype"),
            is_error=bool(message.get("is_error")),
            num_turns=message.get("num_turns"),
            duration_ms=message.get("duration_ms"),
            session_id=message.get("session_id") or self._session_id,
        )
        self._ended = True


def pipe(
    emitter: Emitter, step_id: str | None, stdin: BinaryIO, stdout: BinaryIO,
    fields: dict[str, Any] | None = None,
) -> None:
    """Copy every stdin line to stdout unchanged, emitting events as it goes."""
    parser = ClaudeStreamParser(emitter, step_id, fields)
    writable = True
    for raw in iter(stdin.readline, b""):
        if writable:
            try:
                stdout.write(raw)
                stdout.flush()
            except (BrokenPipeError, OSError):
                writable = False
        try:
            parser.feed(raw.decode("utf-8", errors="replace"))
        except Exception:
            pass
    try:
        parser.finish()
    except Exception:
        pass
