from __future__ import annotations
from typing import Any


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


class ContextManager:
    def __init__(self, token_budget: int = 8000, keep_recent: int = 4):
        self._budget = token_budget
        self._keep_recent = keep_recent
        self._messages: list[dict[str, Any]] = []

    def add_message(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})
        if self.estimated_tokens() > self._budget:
            self._compact()

    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def estimated_tokens(self) -> int:
        return sum(_estimate_tokens(m["content"]) for m in self._messages)

    def _compact(self) -> None:
        if len(self._messages) <= self._keep_recent:
            return
        to_summarize = self._messages[: -self._keep_recent]
        recent = self._messages[-self._keep_recent :]
        summary_text = f"[Compacted {len(to_summarize)} messages]"
        summary_msg = {"role": "system", "content": summary_text}
        self._messages = [summary_msg] + recent
        # If still over budget, keep trimming recent messages (never fewer than 1)
        while self.estimated_tokens() > self._budget and len(self._messages) > 1:
            self._messages.pop(1)
