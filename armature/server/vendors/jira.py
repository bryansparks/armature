"""Jira Cloud connections: Verify with GET /myself, and search issues with JQL, as the email and API token's account."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from armature.server.vendors import (
    POLL_BODY_MAX,
    POLL_TIMEOUT,
    Item,
    VendorError,
    Verified,
    call,
    client,
    json_of,
)

SEARCH_FIELDS = "summary,status,assignee,project,issuetype"
PAGES_MAX = 3


def verify(creds: dict[str, str], config: dict[str, Any], *, now: datetime,
           transport: httpx.BaseTransport | None = None) -> Verified:
    what = "Jira GET /rest/api/3/myself"
    with client(transport) as http:
        found = json_of(call(http, "GET", f"{config['site_url']}/rest/api/3/myself", what,
                             auth=(config["email"], creds["api_token"]), headers={"Accept": "application/json"}),
                        what)
    if not isinstance(found, dict) or not found.get("accountId"):
        raise VendorError(f"{what} named no account.")
    return Verified(identity={"account_id": found["accountId"], "display": found.get("displayName")})


def _name(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _item(site_url: str, issue: dict[str, Any]) -> Item:
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    key = issue.get("key") if isinstance(issue.get("key"), str) else ""
    return Item(key=key, title=fields.get("summary"), url=f"{site_url}/browse/{key}",
                fields={"key": key, "summary": fields.get("summary"), "status": _name(fields.get("status"), "name"),
                        "assignee": _name(fields.get("assignee"), "displayName"),
                        "project": _name(fields.get("project"), "key"),
                        "issuetype": _name(fields.get("issuetype"), "name")})


def search(creds: dict[str, str], config: dict[str, Any], jql: str, *, limit: int,
           transport: httpx.BaseTransport | None = None) -> list[Item]:
    """Up to ``limit`` issues the JQL returns, following at most ``PAGES_MAX`` page tokens."""
    what = "Jira GET /rest/api/3/search/jql"
    found: list[Item] = []
    token = None
    with client(transport, POLL_TIMEOUT) as http:
        for _ in range(PAGES_MAX):
            params = {"jql": jql, "fields": SEARCH_FIELDS, "maxResults": limit - len(found),
                      **({"nextPageToken": token} if token else {})}
            page = json_of(call(http, "GET", f"{config['site_url']}/rest/api/3/search/jql", what,
                                auth=(config["email"], creds["api_token"]), headers={"Accept": "application/json"},
                                params=params, stream=True), what, POLL_BODY_MAX)
            issues = page.get("issues") if isinstance(page, dict) else None
            if not isinstance(issues, list):
                raise VendorError(f"{what} answered without issues.")
            found += [_item(config["site_url"], issue) for issue in issues if isinstance(issue, dict)]
            token = page.get("nextPageToken")
            if len(found) >= limit or not isinstance(token, str) or not token or page.get("isLast"):
                break
    return found[:limit]
