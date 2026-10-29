"""The one place text a model or an outsider wrote is made safe before a tool posts it.

Markdown (GitHub, Linear) loses Armature's own hidden markers, its mentions,
and its raw HTML everywhere, code included, since the model's own fences
can't be trusted to close. Only a fence the server builds, such as a
suggestion's, holds text untouched. Slack mrkdwn is escaped so the text can't form
a link or a mention. Jira text goes out as plain ADF text, so it only loses
markers. Every kind is capped.
"""
from __future__ import annotations

import re

MARKER = re.compile(r"<!--\s*armature-[a-z]+:?.*?-->", re.S)
BACKTICKS = re.compile(r"`+")
MENTION = re.compile(r"@(?=[A-Za-z0-9_])")
LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
MARKDOWN_MAX = 60_000
SLACK_MAX = 3_900
JIRA_MAX = 30_000
QUOTE_MAX = 400
TRUNCATED = "\n\n(truncated)"
ZWSP = "\u200b"


def unmarked(text: str | None) -> str:
    """The text without an ``<!-- armature-... -->`` marker, so a planted one can't misroute a thread."""
    return MARKER.sub("", text or "")


def capped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - len(TRUNCATED)] + TRUNCATED


def markdown(text: str | None, limit: int = MARKDOWN_MAX) -> str:
    """Markdown with no marker, mention, or raw HTML anywhere in it."""
    return capped(MENTION.sub("@" + ZWSP, unmarked(text).replace("<", "&lt;")), limit)


def fence(text: str) -> str:
    """A backtick fence one longer than the longest backtick run in ``text``, so the text can't close it."""
    return "`" * max(3, max((len(m) for m in BACKTICKS.findall(text)), default=0) + 1)


def inert(text: str | None, limit: int = QUOTE_MAX) -> str:
    """One line that can't mention anyone, hide markup, link out, or backlink another repository's issue."""
    text = LINK.sub(r"\1", " ".join(unmarked(text).split()))
    text = text[:limit].replace("<", "&lt;").replace("@", "@" + ZWSP)
    return re.sub(r"#(\d)", "#" + ZWSP + r"\1", text.replace("://", ":/" + ZWSP + "/"))


def quoted(reason: str | None) -> str:
    return "> " + (inert(reason) or "No reason given.")


def slack(text: str | None, limit: int = SLACK_MAX) -> str:
    """mrkdwn whose ``&``, ``<``, and ``>`` are escaped, so no link, user mention, or ``<!channel>`` survives."""
    escaped = unmarked(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return capped(escaped, limit)


def plain(text: str | None, limit: int = JIRA_MAX) -> str:
    return capped(unmarked(text), limit)
