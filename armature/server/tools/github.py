"""GitHub tools: check runs, pull request comments, opening a pull request, and the gate a push must pass.

Each call mints, or reuses, an installation token for the one repository and
the permissions that tool needs. The token never leaves the server.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from armature.server import sanitize
from armature.server.tools import Call, ToolError
from armature.server.vendors import VendorError, client, json_of
from armature.server.vendors import call as vendor_call
from armature.server.vendors import github as vendor

SLUG = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
PAGE = 100
MAX_PAGES = 10
CHECK_NAME = "Armature review"
TITLE_MAX = 250
STATUS_DESCRIPTION_MAX = 140
REVIEW = {"pull_requests": "write", "contents": "read"}
COMMENT = {"pull_requests": "write", "issues": "write"}
CHECKS = {"checks": "write"}
READ_PR = {"pull_requests": "read"}
METADATA = {"metadata": "read"}
WRITERS = ("admin", "write")
DIFF_MAX = 400 * 1024
PUSH_RISKS = ("low", "medium")
NEW_BRANCH_PREFIX = "armature/"
# Every hidden marker Armature writes starts so. A gh CLI login is a person's, so only a marker tells ours apart.
OWN_MARKER = "<!-- armature-"
REPLY_MARKER = "<!-- armature-reply -->"

MEMBER_TTL = timedelta(minutes=10)

_members: dict[tuple[str, str, str], tuple[bool, datetime]] = {}
_members_lock = threading.Lock()
_pr_locks: dict[str, threading.Lock] = {}
_pr_locks_guard = threading.Lock()


def split(repo: str) -> tuple[str, str]:
    if not SLUG.fullmatch(repo or ""):
        raise ToolError(400, "repo must be owner/name.")
    owner, name = repo.split("/")
    return owner, name


class Api:
    """REST calls on one repository as the connection's installation."""

    def __init__(self, call: Call, repo: str, permissions: dict[str, str]):
        self.call = call
        self.repo = repo
        self.base = call.config.get("api_base") or vendor.API
        self._permissions = permissions
        self._token: str | None = None
        self._http = client(call.transport)

    def close(self) -> None:
        self._http.close()

    def token(self) -> str:
        if self._token is None:
            if self.call.installation_id is None and not vendor.uses_gh_cli(self.call.config):
                raise ToolError(409, f"The GitHub App has no installation on {split(self.repo)[0]}.")
            self._token = vendor.mint(connection_id=self.call.connection.id, creds=self.call.creds,
                                      config=self.call.config, installation_id=self.call.installation_id,
                                      repo=split(self.repo)[1], permissions=self._permissions, now=self.call.now,
                                      transport=self.call.transport)
        return self._token

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return vendor_call(self._http, method, f"{self.base}{path}", f"GitHub {method} {path.split('?')[0]}",
                           headers=vendor.headers(self.token()), **kwargs)

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        return json_of(self.request(method, path, **kwargs), f"GitHub {method} {_shape(path)}")

    def pages(self, path: str, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self.json("GET", path, params={"per_page": PAGE, "page": page})
            if not isinstance(batch, list):
                raise VendorError(f"GitHub GET {_shape(path)} did not answer with a list.")
            found += [item for item in batch if isinstance(item, dict)]
            if len(batch) < PAGE:
                break
        return found


def _shape(path: str) -> str:
    return re.sub(r"/\d+(?=/|$)", "/{n}", path.split("?")[0])


@contextmanager
def api(call: Call, repo: str, permissions: dict[str, str]) -> Iterator[Api]:
    found = Api(call, repo, permissions)
    try:
        yield found
    finally:
        found.close()


@contextmanager
def pr_lock(repo: str, number: int) -> Iterator[None]:
    """One writer at a time per pull request in this process, so two runs never post two sticky comments."""
    with _pr_locks_guard:
        lock = _pr_locks.setdefault(f"{repo.lower()}#{number}", threading.Lock())
    with lock:
        yield


def me(call: Call) -> str:
    return str(call.identity.get("bot_login") or call.identity.get("login") or "").lower()


def ours(call: Call) -> Callable[[Any], bool]:
    """Whether a comment or review is this connection's own: its login, and for a gh CLI login also a marker."""
    mine, marked = me(call), vendor.uses_gh_cli(call.config)
    return lambda item: bool(mine) and login_of(item) == mine \
        and (not marked or OWN_MARKER in str((item or {}).get("body") or ""))


def reply_body(call: Call, text: str) -> str:
    return f"{text}\n\n{REPLY_MARKER}" if vendor.uses_gh_cli(call.config) else text


def login_of(item: Any) -> str:
    user = item.get("user") if isinstance(item, dict) else None
    return str((user or {}).get("login") or "").lower() if isinstance(user, dict) else ""


def check_run(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """github.check-run: any check but the reviewed mark, which a step could otherwise forge."""
    if str(params.get("name") or "").strip().lower() in ("", CHECK_NAME.lower()):
        raise ToolError(403, f"{CHECK_NAME} is the server's own mark. Name the check run something else.")
    return upsert_check(call, params)


def upsert_check(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Create the named check run on the head, or update the one this app already made there."""
    repo, head = params["repo"], params["head"]
    name = params.get("name") or CHECK_NAME
    status = params.get("status") or ("completed" if params.get("conclusion") else "in_progress")
    if status == "completed" and not params.get("conclusion"):
        raise ToolError(400, "A completed check run needs a conclusion.")
    body: dict[str, Any] = {"name": name, "status": status}
    if status == "completed":
        body["conclusion"] = params["conclusion"]
    if call.run_url.startswith("https://"):
        body["details_url"] = call.run_url
    if params.get("title") or params.get("summary"):
        body["output"] = {"title": sanitize.plain(params.get("title") or name, TITLE_MAX),
                          "summary": sanitize.markdown(params.get("summary") or "")}
    if vendor.uses_gh_cli(call.config):
        return _post_status(call, repo, head, body)
    slug = call.identity.get("slug")
    with api(call, repo, CHECKS) as gh:
        found = gh.json("GET", f"/repos/{repo}/commits/{head}/check-runs",
                        params={"check_name": name, "per_page": PAGE})
        runs = found.get("check_runs") if isinstance(found, dict) else None
        ours = [r for r in runs or [] if isinstance(r, dict) and r.get("head_sha") == head
                and (r.get("app") or {}).get("slug") == slug]
        if ours:
            made = gh.json("PATCH", f"/repos/{repo}/check-runs/{int(ours[0]['id'])}", json=body)
        else:
            made = gh.json("POST", f"/repos/{repo}/check-runs", json={**body, "head_sha": head})
    return {"check_run_id": made.get("id"), "url": made.get("html_url"), "created": not ours, "status": status,
            "conclusion": body.get("conclusion")}


def _post_status(call: Call, repo: str, head: str, check: dict[str, Any]) -> dict[str, Any]:
    """A commit status standing in for a check run, since only an App may make check runs. Each post is new."""
    conclusion = check.get("conclusion")
    state = "pending" if check["status"] != "completed" else "success" if conclusion == "success" else "failure"
    body = {"state": state, "context": check["name"],
            "description": sanitize.plain((check.get("output") or {}).get("title") or check["name"],
                                          STATUS_DESCRIPTION_MAX)}
    if check.get("details_url"):
        body["target_url"] = check["details_url"]
    with api(call, repo, CHECKS) as gh:
        made = gh.json("POST", f"/repos/{repo}/statuses/{head}", json=body)
    return {"check_run_id": made.get("id"), "url": body.get("target_url"), "created": True,
            "status": check["status"], "conclusion": conclusion}


def permission(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Whether user may write to repo, as GitHub says through the installation. Anything but write or admin is no."""
    repo, user = params["repo"], params["user"]
    with api(call, repo, METADATA) as gh:
        response = gh.request("GET", f"/repos/{repo}/collaborators/{quote(user)}/permission")
        level = "none" if response.status_code == 404 else \
            str(json_of(response, "GitHub GET /repos/{repo}/collaborators/{user}/permission").get("permission"))
    return {"repo": repo, "user": user, "permission": level, "allowed": level in WRITERS}


def pr_diff(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """A pull request's changed files and patches, cut off at DIFF_MAX bytes of patch text."""
    repo, number = params["repo"], int(params["number"])
    files, size, cut = [], 0, False
    with api(call, repo, READ_PR) as gh:
        pr = gh.json("GET", f"/repos/{repo}/pulls/{number}")
        for f in gh.pages(f"/repos/{repo}/pulls/{number}/files"):
            patch = f.get("patch") or ""
            if size + len(patch.encode()) > DIFF_MAX:
                cut, patch = True, ""
            size += len(patch.encode())
            files.append({"filename": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"),
                          "deletions": f.get("deletions"), "patch": patch})
    return {"repo": repo, "number": number, "title": pr.get("title") or "", "head": (pr.get("head") or {}).get("sha"),
            "files": files, "truncated": cut}


def pr_comment(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """A new comment, a reply in one of the app's own review threads, or an edit of the app's one marked comment."""
    repo, number = params["repo"], int(params["number"])
    text = sanitize.markdown(params["body"])
    own = ours(call)
    with api(call, repo, COMMENT) as gh:
        if params.get("in_reply_to"):
            root_id = int(params["in_reply_to"])
            root = gh.json("GET", f"/repos/{repo}/pulls/comments/{root_id}")
            if not own(root) or root.get("in_reply_to_id"):
                raise ToolError(403, "A reply goes only in a thread that starts with this app's own comment.")
            made = gh.json("POST", f"/repos/{repo}/pulls/{number}/comments/{root_id}/replies",
                           json={"body": reply_body(call, text)})
            return {"comment_id": made.get("id"), "url": made.get("html_url"), "updated": False}
        if not params.get("marker"):
            made = gh.json("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": text})
            return {"comment_id": made.get("id"), "url": made.get("html_url"), "updated": False}
        marker = f"<!-- armature-{params['marker']} -->"
        body = f"{marker}\n{text}"
        with pr_lock(repo, number):
            # A comment another identity posted stays as it is, and this app posts its own.
            found = [c for c in gh.pages(f"/repos/{repo}/issues/{number}/comments")
                     if own(c) and marker in (c.get("body") or "")]
            if found:
                existing = found[-1]
                if existing.get("body") == body:
                    return {"comment_id": existing.get("id"), "url": existing.get("html_url"), "updated": False}
                response = gh.request("PATCH", f"/repos/{repo}/issues/comments/{int(existing['id'])}",
                                      json={"body": body})
                if response.status_code != 404:
                    made = json_of(response, "GitHub PATCH /repos/{n}/issues/comments")
                    return {"comment_id": made.get("id"), "url": made.get("html_url"), "updated": True}
            made = gh.json("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
    return {"comment_id": made.get("id"), "url": made.get("html_url"), "updated": False}


def open_pr(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Open the pull request, only from the commit the run's review rated, so it can't rate one branch and open
    another."""
    repo = params["repo"]
    with api(call, repo, {**READ_PR, "contents": "read"}) as gh:
        found = gh.request("GET", f"/repos/{repo}/branches/{quote(params['head'], safe='')}")
        head = ((json_of(found, "GitHub GET /repos/{n}/branches").get("commit") or {}).get("sha")) \
            if found.status_code == 200 else None
    if not head or head != call.policy.get("head_sha"):
        raise ToolError(409, "The branch's head is not the commit this run's review rated.")
    body = {"title": sanitize.plain(params["title"], TITLE_MAX).replace("\n", " "), "head": params["head"],
            "base": params["base"], "body": sanitize.markdown(params.get("body") or ""),
            "draft": bool(params.get("draft"))}
    with api(call, repo, COMMENT) as gh:
        made = gh.json("POST", f"/repos/{repo}/pulls", json=body)
        number = made.get("number")
        requested, request_error = [], None
        reviewers = [r for r in params.get("reviewers") or [] if r.lower() != me(call)]
        if reviewers and isinstance(number, int):
            try:
                gh.json("POST", f"/repos/{repo}/pulls/{number}/requested_reviewers", json={"reviewers": reviewers})
                requested = reviewers
            except VendorError as exc:
                request_error = str(exc)
    return {"number": number, "url": made.get("html_url"), "requested_reviewers": requested,
            "request_error": request_error}


def _team(entry: str, owner: str) -> tuple[str, str]:
    """A ``team:<slug>`` or ``team:<org>/<slug>`` entry as (org, slug). A bare slug is in the repository's owner."""
    org, _, slug = entry[len("team:"):].rpartition("/")
    return org or owner, slug


def team_member(gh: Api, org: str, slug: str, author: str) -> bool:
    """Whether the author is an active member, from GitHub, remembered ten minutes. Any error answers no."""
    key = (org.lower(), slug.lower(), author.lower())
    now = gh.call.now
    with _members_lock:
        hit = _members.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
    try:
        response = gh.request("GET", f"/orgs/{quote(org)}/teams/{quote(slug)}/memberships/{quote(author)}")
        if response.status_code not in (200, 404):
            return False
        member = response.status_code == 200 and response.json().get("state") == "active"
    except (VendorError, ValueError, AttributeError):
        return False
    with _members_lock:
        _members[key] = (member, now + MEMBER_TTL)
    return member


def author_allowed(gh: Api, author: str, requested_teams: list[str], allow: list[str],
                   exclude: list[str]) -> tuple[bool, str]:
    """ops pr_authors.sh's order: a requested reviewing team, then the exclude list, then team, login, or ``*``."""
    owner = gh.repo.split("/")[0]
    login = author.lower()
    asked = {t.lower() for t in requested_teams}
    for entry in allow:
        if entry.startswith("team:"):
            org, slug = _team(entry, owner)
            if org.lower() == owner.lower() and slug.lower() in asked:
                return True, f"codeowner-request:{slug}"
    if login in {e.lower() for e in exclude}:
        return False, f"{author} is on the push exclude list"
    for entry in allow:
        if entry == "*":
            return True, "*"
        if entry.startswith("team:"):
            if team_member(gh, *_team(entry, owner), author):
                return True, entry
        elif entry.lower() == login:
            return True, f"login:{entry}"
    return False, f"{author} is not on the push allowlist"


@contextmanager
def _pr_api(call: Call, repo: str, allow: list[str]) -> Iterator[tuple[Api, bool]]:
    """The pull request reader, with org membership when a team entry needs it and the installation grants it.

    Without that permission team entries match nothing, so the gate fails closed.
    """
    if any(e.startswith("team:") for e in allow):
        with api(call, repo, {**READ_PR, "members": "read"}) as gh:
            try:
                gh.token()
            except VendorError:
                pass
            else:
                yield gh, True
                return
    with api(call, repo, READ_PR) as gh:
        yield gh, False


def new_branch_gate(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Whether this step may create a branch: one under armature/ that doesn't exist and has no pull request yet.

    It needs no recorded risk, since a pull request from it must pass the review before it can open.
    """
    repo, branch, commit = params["repo"], params["branch"], params.get("commit")
    refused = {"approved": False, "repo": repo, "number": None, "branch": branch}
    if not branch.startswith(NEW_BRANCH_PREFIX) or ".." in branch:
        return {**refused, "reason": f"a push without a pull request goes only to a new {NEW_BRANCH_PREFIX} branch"}
    owner = split(repo)[0]
    with api(call, repo, {**READ_PR, "contents": "read"}) as gh:
        found = gh.request("GET", f"/repos/{repo}/branches/{quote(branch, safe='')}")
        if found.status_code != 404:
            return {**refused, "reason": "the branch already exists"}
        prs = gh.json("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all",
                                                              "per_page": 1})
        if not isinstance(prs, list) or prs:
            return {**refused, "reason": "a pull request already uses the branch"}
    return {"approved": True, "repo": repo, "number": None, "branch": branch, "rule": "new branch",
            "commit": commit}


def push_gate(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """Whether this step may push to the pull request's branch. Only GitHub's answers and settings decide."""
    if not params.get("number"):
        return new_branch_gate(call, params)
    repo, number, branch = params["repo"], int(params["number"]), params["branch"]
    policy = call.policy
    refused = {"approved": False, "repo": repo, "number": number, "branch": branch}
    commit = params.get("commit")
    # The risk the server recorded for this run. Anything but low or medium is refused.
    if not policy.get("risk"):
        return {**refused, "reason": "the run has no recorded risk"}
    if policy.get("risk") not in PUSH_RISKS:
        return {**refused, "reason": "the run's risk is high"}
    allow, exclude = policy.get("authors") or [], policy.get("exclude") or []
    if not allow:
        return {**refused, "reason": "no push author allowlist is set"}
    with _pr_api(call, repo, allow) as (gh, teams_ok):
        pr = gh.json("GET", f"/repos/{repo}/pulls/{number}")
        head, base = pr.get("head") or {}, pr.get("base") or {}
        head_repo = str(((head.get("repo") or {}).get("full_name")) or "").lower()
        base_repo = str(((base.get("repo") or {}).get("full_name")) or "").lower()
        if pr.get("state") != "open":
            return {**refused, "reason": "the pull request is not open"}
        if head_repo != repo.lower() or base_repo != repo.lower():
            return {**refused, "reason": "the pull request comes from a fork"}
        if head.get("ref") != branch:
            return {**refused, "reason": "the branch is not the pull request's head branch"}
        author = str((pr.get("user") or {}).get("login") or "")
        teams = [t.get("slug") for t in pr.get("requested_teams") or [] if isinstance(t, dict) and t.get("slug")]
        allowed, rule = author_allowed(gh, author, teams, allow if teams_ok else
                                       [e for e in allow if not e.startswith("team:")], exclude)
    if not allowed:
        return {**refused, "reason": rule}
    return {"approved": True, "repo": repo, "number": number, "branch": branch, "head": head.get("sha"),
            "author": author, "rule": rule, "commit": commit}


def git_token(call: Call, repo: str, write: bool) -> str:
    """A fresh token for git on one repository, never cached, so the run's end can revoke it."""
    if call.installation_id is None and not vendor.uses_gh_cli(call.config):
        raise ToolError(409, f"The GitHub App has no installation on {split(repo)[0]}.")
    return vendor.mint(connection_id=call.connection.id, creds=call.creds, config=call.config,
                       installation_id=call.installation_id, repo=split(repo)[1],
                       permissions={"contents": "write" if write else "read"}, now=call.now,
                       transport=call.transport, cache=False)
