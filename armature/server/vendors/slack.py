"""Slack connections: Verify with auth.test. The signing secret is checked on the first delivery."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from armature.server.vendors import VendorError, Verified, call, client, json_of

API = "https://slack.com/api"
REQUIRED_SCOPES = ("chat:write",)
CODE = re.compile(r"^[a-z0-9_]{1,64}$")


def verify(creds: dict[str, str], config: dict[str, Any], *, now: datetime,
           transport: httpx.BaseTransport | None = None) -> Verified:
    what = "Slack auth.test"
    with client(transport) as http:
        response = call(http, "POST", f"{API}/auth.test", what,
                        headers={"Authorization": f"Bearer {creds['bot_token']}"})
        found = json_of(response, what)
    if not isinstance(found, dict):
        raise VendorError(f"{what} did not answer with an object.")
    if not found.get("ok"):
        code = found.get("error")
        raise VendorError(f"{what} refused the token: {code}." if isinstance(code, str) and CODE.fullmatch(code)
                          else f"{what} refused the token.")
    scopes = sorted(s.strip() for s in response.headers.get("x-oauth-scopes", "").split(",") if s.strip())
    problems = [f"The token lacks the {s} scope." for s in REQUIRED_SCOPES if s not in scopes]
    team_id = found.get("team_id")
    if config.get("team_id") and team_id != config["team_id"]:
        problems.append(f"The token belongs to workspace {team_id}, not {config['team_id']}.")
    identity = {"team": found.get("team"), "team_id": team_id, "bot_user": found.get("user"),
                "bot_user_id": found.get("user_id"), "bot_id": found.get("bot_id"), "scopes": scopes}
    return Verified(identity=identity, problems=problems)
