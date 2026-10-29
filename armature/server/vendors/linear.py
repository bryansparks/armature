"""Linear connections: Verify by asking GraphQL who the API key is, and search a team's issues."""
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

API = "https://api.linear.app/graphql"
QUERY = "query { viewer { id name } organization { id name urlKey } }"
SEARCH = ("query Issues($filter: IssueFilter, $first: Int) { issues(filter: $filter, first: $first, "
          "orderBy: createdAt) { nodes { identifier title url state { name type } assignee { name } team { key } } } }")


def verify(creds: dict[str, str], config: dict[str, Any], *, now: datetime,
           transport: httpx.BaseTransport | None = None) -> Verified:
    what = "Linear GraphQL"
    with client(transport) as http:
        found = json_of(call(http, "POST", API, what, json={"query": QUERY},
                             headers={"Authorization": creds["api_key"]}), what)
    data = found.get("data") if isinstance(found, dict) else None
    if not isinstance(data, dict) or found.get("errors") or not isinstance(data.get("viewer"), dict):
        raise VendorError(f"{what} refused the key.")
    viewer, org = data["viewer"], data.get("organization") if isinstance(data.get("organization"), dict) else {}
    return Verified(identity={"user": viewer.get("name"), "user_id": viewer.get("id"), "org": org.get("name"),
                              "org_id": org.get("id"), "org_key": org.get("urlKey")})


def issue_filter(query: dict[str, Any], number: int | None = None) -> dict[str, Any]:
    """The GraphQL IssueFilter for a poll's team, state types, assignee, and label."""
    found: dict[str, Any] = {"team": {"key": {"eq": query["team"]}}}
    if query.get("states"):
        found["state"] = {"type": {"in": query["states"]}}
    if query.get("assignee") == "me":
        found["assignee"] = {"isMe": {"eq": True}}
    if query.get("label"):
        found["labels"] = {"some": {"name": {"eq": query["label"]}}}
    if number is not None:
        found["number"] = {"eq": number}
    return found


def _name(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def search(creds: dict[str, str], query: dict[str, Any], *, limit: int, number: int | None = None,
           transport: httpx.BaseTransport | None = None) -> list[Item]:
    what = "Linear GraphQL issues"
    with client(transport, POLL_TIMEOUT) as http:
        found = json_of(call(http, "POST", API, what, headers={"Authorization": creds["api_key"]}, stream=True,
                             json={"query": SEARCH, "variables": {"filter": issue_filter(query, number),
                                                                  "first": limit}}), what, POLL_BODY_MAX)
    data = found.get("data") if isinstance(found, dict) else None
    issues = data.get("issues") if isinstance(data, dict) else None
    nodes = issues.get("nodes") if isinstance(issues, dict) else None
    if not isinstance(nodes, list) or found.get("errors"):
        raise VendorError(f"{what} refused the query.")
    out = []
    for node in nodes[:limit]:
        if not isinstance(node, dict) or not isinstance(node.get("identifier"), str):
            continue
        out.append(Item(key=node["identifier"], title=node.get("title"), url=node.get("url"),
                        fields={"key": node["identifier"], "title": node.get("title"), "url": node.get("url"),
                                "state": _name(node.get("state"), "name"),
                                "state_type": _name(node.get("state"), "type"),
                                "assignee": _name(node.get("assignee"), "name"),
                                "team": _name(node.get("team"), "key")}))
    return out
