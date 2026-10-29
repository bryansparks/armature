"""Jira tools: read an issue, create one in the project's Jira project, comment, and move an issue to a status with
ops jira_transition.sh's guards, on REST v3."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from armature.server import sanitize
from armature.server.tools import Call, ToolError
from armature.server.vendors import VendorError, call, client, json_of

KEY = re.compile(r"[A-Z][A-Z0-9]+-[0-9]+")
PROJECT_KEY = "jira.project"
SUMMARY_MAX = 255
ISSUE_FIELDS = "summary,description,status,issuetype,components,labels,comment"
COMMENTS_MAX = 50
# The description and each comment, so one long issue can't crowd the code out of a session's context.
BODY_MAX = 4000


def _lines(block: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for index, line in enumerate(block.split("\n")):
        if index:
            content.append({"type": "hardBreak"})
        if line:
            content.append({"type": "text", "text": line})
    return content


def adf(text: str) -> dict[str, Any]:
    """Plain text as Atlassian Document Format, so nothing in it is markup.

    Each blank-line block is a paragraph, and each line break in it a hard break, so lines stay apart in Jira.
    """
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", sanitize.plain(text).replace("\r\n", "\n"))
              if b.strip()] or ["(empty)"]
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": _lines(b)} for b in blocks]}


class Jira:
    def __init__(self, ctx: Call):
        self.site = ctx.config["site_url"]
        self._auth = (ctx.config["email"], ctx.creds["api_token"])
        self._http = client(ctx.transport)

    def __enter__(self) -> Jira:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        what = f"Jira {method} {re.sub(KEY, '{key}', path.split('?')[0])}"
        return call(self._http, method, f"{self.site}/rest/api/3{path}", what, auth=self._auth,
                    headers={"Accept": "application/json"}, **kwargs)

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        return json_of(response, f"Jira {method} {re.sub(KEY, '{key}', path.split('?')[0])}") \
            if response.status_code != 204 else {}


def text_of(doc: Any) -> str:
    """Atlassian Document Format as plain text: paragraphs apart by a blank line, a hard break as a newline."""
    if not isinstance(doc, dict):
        return doc if isinstance(doc, str) else ""
    if doc.get("type") == "text":
        return str(doc.get("text") or "")
    if doc.get("type") == "hardBreak":
        return "\n"
    parts = [text_of(child) for child in doc.get("content") or []]
    return ("\n\n" if doc.get("type") == "doc" else "").join(parts)


def _capped(text: str) -> str:
    return text[:BODY_MAX] + "\n[truncated]" if len(text) > BODY_MAX else text


def get_issue(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = params["issue"]
    with Jira(ctx) as jira:
        response = jira.request("GET", f"/issue/{quote(issue)}", params={"fields": ISSUE_FIELDS})
        if response.status_code in (403, 404):
            return {"issue": issue, "found": False}
        fields = json_of(response, "Jira GET /issue/{key}").get("fields") or {}
    comments = ((fields.get("comment") or {}).get("comments") or [])[-COMMENTS_MAX:]
    return {"issue": issue, "found": True, "summary": fields.get("summary") or "",
            "description": _capped(text_of(fields.get("description"))),
            "status": (fields.get("status") or {}).get("name") or "",
            "type": (fields.get("issuetype") or {}).get("name") or "",
            "components": [c.get("name") for c in fields.get("components") or [] if isinstance(c, dict)],
            "labels": [x for x in fields.get("labels") or [] if isinstance(x, str)],
            "comments": [{"author": (c.get("author") or {}).get("displayName") or "", "body": _capped(text_of(c.get("body")))}
                         for c in comments if isinstance(c, dict)],
            "url": f"{ctx.config['site_url']}/browse/{issue}"}


def create(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    """An issue in the project's own Jira project, from an allowlist of fields. The project key is never a param."""
    project = ctx.policy.get("project")
    if not isinstance(project, str) or not re.fullmatch(r"[A-Z][A-Z0-9]+", project):
        raise ToolError(409, f"Set the {PROJECT_KEY} setting to this project's Jira project key first.")
    fields: dict[str, Any] = {"project": {"key": project}, "issuetype": {"name": params["type"]},
                              "summary": " ".join(sanitize.plain(params["summary"]).split())[:SUMMARY_MAX]}
    if params.get("description"):
        fields["description"] = adf(params["description"])
    if params.get("labels"):
        fields["labels"] = params["labels"]
    with Jira(ctx) as jira:
        made = jira.json("POST", "/issue", json={"fields": fields})
    key = made.get("key") if isinstance(made, dict) else None
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise VendorError("Jira POST /issue answered without an issue key.")
    return {"issue": key, "created": True, "url": f"{ctx.config['site_url']}/browse/{key}"}


def comment(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = params["issue"]
    with Jira(ctx) as jira:
        made = jira.json("POST", f"/issue/{quote(issue)}/comment", json={"body": adf(params["body"])})
    return {"issue": issue, "commented": True, "comment_id": made.get("id")}


def _key(params: dict[str, Any]) -> str | None:
    if params.get("issue"):
        return params["issue"]
    for text in params.get("key_from") or []:
        found = KEY.search(text or "")
        if found:
            return found.group(0)
    return None


def transition(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = _key(params)
    if issue is None:
        # Plenty of pull requests name no ticket, so this is data for the workflow, not a failure.
        return {"issue": None, "transitioned": False, "reason": "no issue key found"}
    projects = params.get("projects") or []
    if projects and issue.rsplit("-", 1)[0] not in projects:
        return {"issue": issue, "transitioned": False, "reason": "project not in projects"}
    with Jira(ctx) as jira:
        response = jira.request("GET", f"/issue/{quote(issue)}", params={"fields": "status,issuetype"})
        if response.status_code in (403, 404):
            return {"issue": issue, "transitioned": False, "reason": "issue not readable"}
        fields = (json_of(response, "Jira GET /issue/{key}").get("fields") or {})
        issue_type = str((fields.get("issuetype") or {}).get("name") or "")
        current = str((fields.get("status") or {}).get("name") or "")
        defect = issue_type == "Defect" and bool(params.get("defect_to"))
        target = params["defect_to"] if defect else params["to"]
        resolution = params.get("defect_resolution") if defect else None
        result = {"issue": issue, "from": current, "to": target, "issue_type": issue_type}
        if current.lower() == target.lower():
            return {**result, "transitioned": False, "reason": "already in target status"}
        # A status past the from guard means a person or an earlier delivery already moved it.
        if params.get("from") and current.lower() != params["from"].lower():
            return {**result, "transitioned": False, "reason": f"source status is {current}, not {params['from']}"}
        found = jira.json("GET", f"/issue/{quote(issue)}/transitions").get("transitions") or []
        move = next((t for t in found if isinstance(t, dict) and target.lower() in
                     (str(t.get("name") or "").lower(), str((t.get("to") or {}).get("name") or "").lower())), None)
        if move is None:
            return {**result, "transitioned": False, "reason": "transition unavailable"}
        body: dict[str, Any] = {"transition": {"id": str(move.get("id"))}}
        if resolution:
            body["fields"] = {"resolution": {"name": resolution}}
        moved = jira.request("POST", f"/issue/{quote(issue)}/transitions", json=body)
        if moved.status_code in (400, 409):
            return {**result, "transitioned": False, "reason": "transition unavailable"}
        if moved.status_code >= 300:
            raise VendorError(f"Jira POST /issue/{{key}}/transitions answered HTTP {moved.status_code}.")
        comment_error = None
        if params.get("comment"):
            try:
                jira.json("POST", f"/issue/{quote(issue)}/comment", json={"body": adf(params["comment"])})
            except VendorError as exc:
                comment_error = str(exc)
    return {**result, "transitioned": True, "url": f"{ctx.config['site_url']}/browse/{issue}",
            **({"comment_error": comment_error} if comment_error else {})}
