"""Turns Claude Code `--output-format stream-json --verbose` lines into events."""
from __future__ import annotations

import json
from typing import Any, BinaryIO

from armature.events.emitter import Emitter

SUMMARY_CHARS = 2048


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


class ClaudeStreamParser:
    def __init__(self, emitter: Emitter, step_id: str | None = None):
        self._emitter = emitter
        self._step_id = step_id
        self._tool_names: dict[str, str] = {}
        self._started = False
        self._ended = False
        self._model: str | None = None
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
        self._emitter.emit(kind, self._step_id, **payload)

    def _summary(self, value: Any) -> str:
        # Redact before cutting, so a cut can't leave part of a secret behind.
        return self._emitter.redact({"text": _text(value)}).get("text", "")[:SUMMARY_CHARS]

    def _on_system(self, message: dict[str, Any]) -> None:
        if message.get("subtype") != "init" or self._started:
            return
        self._started = True
        self._model = message.get("model")
        self._session_id = message.get("session_id")
        self._emit("session_start", model=self._model, session_id=self._session_id)

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
        self._emit(
            "cost",
            model=self._model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=message.get("total_cost_usd") or 0.0,
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            session_id=message.get("session_id") or self._session_id,
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


def pipe(emitter: Emitter, step_id: str | None, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Copy every stdin line to stdout unchanged, emitting events as it goes."""
    parser = ClaudeStreamParser(emitter, step_id)
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
