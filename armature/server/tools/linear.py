"""Linear tools over GraphQL: comment, and move an issue to its team's state of a name."""
from __future__ import annotations

from typing import Any

from armature.server import sanitize
from armature.server.tools import Call
from armature.server.vendors import VendorError, call, client, json_of
from armature.server.vendors.linear import API

ISSUE = "query($id: String!) { issue(id: $id) { id team { states { nodes { id name } } } } }"
COMMENT = "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"
UPDATE = "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }"


def _gql(ctx: Call, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    what = "Linear GraphQL"
    with client(ctx.transport) as http:
        found = json_of(call(http, "POST", API, what, json={"query": query, "variables": variables},
                             headers={"Authorization": ctx.creds["api_key"]}), what)
    data = found.get("data") if isinstance(found, dict) else None
    if not isinstance(data, dict) or found.get("errors"):
        raise VendorError(f"{what} refused the request.")
    return data


def _issue(ctx: Call, ident: str) -> dict[str, Any]:
    issue = _gql(ctx, ISSUE, {"id": ident}).get("issue")
    if not isinstance(issue, dict) or not issue.get("id"):
        raise VendorError(f"Linear has no issue {ident}.")
    return issue


def comment(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = _issue(ctx, params["issue"])
    done = _gql(ctx, COMMENT, {"input": {"issueId": issue["id"], "body": sanitize.markdown(params["body"])}})
    if not (done.get("commentCreate") or {}).get("success"):
        raise VendorError("Linear commentCreate failed.")
    return {"issue": params["issue"], "commented": True}


def transition(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = _issue(ctx, params["issue"])
    wanted = params["state"].lower()
    states = ((issue.get("team") or {}).get("states") or {}).get("nodes") or []
    state = next((s for s in states if isinstance(s, dict) and str(s.get("name") or "").lower() == wanted), None)
    if state is None:
        return {"issue": params["issue"], "moved": False, "reason": f"the team has no state named {params['state']}"}
    done = _gql(ctx, UPDATE, {"id": issue["id"], "input": {"stateId": state["id"]}})
    if not (done.get("issueUpdate") or {}).get("success"):
        raise VendorError("Linear issueUpdate failed.")
    return {"issue": params["issue"], "moved": True, "state": state.get("name")}
