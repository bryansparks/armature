"""Slack tools: post a message, or in a thread, and replace one the bot posted. Text is always escaped."""
from __future__ import annotations

from typing import Any

from armature.server import sanitize
from armature.server.tools import Call
from armature.server.vendors import VendorError, call, client, json_of
from armature.server.vendors.slack import API, CODE


def _send(ctx: Call, method: str, body: dict[str, Any]) -> dict[str, Any]:
    what = f"Slack {method}"
    with client(ctx.transport) as http:
        found = json_of(call(http, "POST", f"{API}/{method}", what, json=body,
                             headers={"Authorization": f"Bearer {ctx.creds['bot_token']}"}), what)
    if not isinstance(found, dict) or not found.get("ok"):
        code = found.get("error") if isinstance(found, dict) else None
        raise VendorError(f"{what} refused the message: {code}." if isinstance(code, str) and CODE.fullmatch(code)
                          else f"{what} refused the message.")
    return found


def post(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    unfurl = params.get("unfurl") is True
    body: dict[str, Any] = {"channel": params["channel"], "text": sanitize.slack(params["text"]),
                            "unfurl_links": unfurl, "unfurl_media": unfurl}
    if params.get("thread_ts"):
        body["thread_ts"] = params["thread_ts"]
        if params.get("reply_broadcast"):
            body["reply_broadcast"] = True
    found = _send(ctx, "chat.postMessage", body)
    return {"posted": True, "channel": found.get("channel"), "ts": found.get("ts")}


def update(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    found = _send(ctx, "chat.update", {"channel": params["channel"], "ts": params["ts"],
                                       "text": sanitize.slack(params["text"])})
    return {"updated": True, "channel": found.get("channel"), "ts": found.get("ts")}


# The server's own messages, which no run can send: a gate's card and the notes its buttons answer with.

def card(ctx: Call, channel: str, text: str, blocks: list[dict[str, Any]], thread_ts: str | None = None) -> str | None:
    """Post a card whose blocks the server built, with ``text`` as its notification. Returns its ts."""
    body: dict[str, Any] = {"channel": channel, "text": sanitize.slack(text), "blocks": blocks,
                            "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        body["thread_ts"] = thread_ts
    return _send(ctx, "chat.postMessage", body).get("ts")


def replace_card(ctx: Call, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]) -> None:
    _send(ctx, "chat.update", {"channel": channel, "ts": ts, "text": sanitize.slack(text), "blocks": blocks})


def ephemeral(ctx: Call, channel: str, user: str, text: str) -> None:
    """A note only ``user`` sees in ``channel``."""
    _send(ctx, "chat.postEphemeral", {"channel": channel, "user": user, "text": sanitize.slack(text)})
