"""pr-fix: ``/armature fix`` in a review thread applies Armature's latest review, reviews the fix before it reaches
the pull request, and pushes it there only when that review approves at low or medium risk.

Replaces ops' fix branch of ``/gh/pr`` and specs/pr-fix.yml. The stages come from S14b's starters: ``review-pass``
for the reviews that post, and ``code-review``'s fix, push, and decisions steps. ``review-branch`` is review-pass
reviewing a pushed branch instead of the pull request's head, posting and marking nothing, so a fix is reviewed on a
new ``armature/fix-`` branch first and the pull request moves only on approval.
"""
from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from ruamel.yaml import YAML

from armature.server import starters
from armature.server.migrate_ops import Rows, Rule, Webhook

COMMAND = "/armature fix"
WEBHOOK = Webhook("github", "github")
NEXT = ("Only a reply in a review thread triggers this. ops also took /armature fix as a pull request comment or a "
        "review body, which need a pull request read the rules don't have yet.")
PREVIEW = "armature/fix-{{ number }}-{{ fix.commit }}"
# review-pass's steps that post to or mark the pull request, which a branch review must never do.
POSTING = ("marked", "decisions", "post", "mark")


def _yaml() -> YAML:
    y = YAML()
    y.width = 4096
    return y


def _dump(data: Any) -> str:
    out = io.StringIO()
    _yaml().dump(data, out)
    return out.getvalue()


def review_branch() -> str:
    """review-pass for a pushed branch: rated and risk-recorded by branch and base, reviewed, and posted nowhere."""
    data = _yaml().load(starters.body("review-pass"))
    data["name"] = "review-branch"
    data["description"] = ("Review a pushed branch with the specialist panel and the team lead, without posting to or "
                           "marking its pull request.")
    inputs = data["contracts"]["inputs"]
    inputs.insert(len(inputs) - 1, {"name": "base", "type": "string", "required": True})
    data["contracts"]["outputs"] = [o for o in data["contracts"]["outputs"] if o["name"] not in ("marked", "head")]
    data["contracts"]["outputs"].append({"name": "head", "type": "string", "from": "{{ risk.head }}"})
    data["stages"] = [s for s in data["stages"] if s["id"] not in POSTING]
    for stage in data["stages"]:
        skip = stage.get("skip_if")
        if skip == "{{ marked.valid }}":
            del stage["skip_if"]
        elif isinstance(skip, str):
            stage["skip_if"] = skip.replace("marked.valid or ", "")
        if stage.get("depends_on") == ["marked"]:
            stage["depends_on"] = []
        if stage["id"] == "risk":
            stage["with"] = {"repo": "{{ slug }}", "branch": "{{ ref }}", "base": "{{ base }}",
                             "level": "{{ rate.level }}", "reasons": "{{ rate.reasons }}",
                             "expect_head": "{{ rate.commit }}"}
    return _dump(data)


def body() -> str:
    code = {s["id"]: s for s in _yaml().load(starters.body("code-review"))["stages"]}
    pushable, fix, push, fixed, second = (code[k] for k in ("pushable", "fix", "push", "fixed", "second"))
    pushable["skip_if"] = ("{{ not permission.allowed or first.marked or first.verdict == 'approve' "
                           "or not first.comments }}")
    preview = {"id": "preview", "skip_if": "{{ not fix.built or not fix.fixed or fix.commit == ref }}",
               "adapter": "github/push",
               "with": {"repo": "{{ slug }}", "branch": PREVIEW, "commit": "{{ fix.commit }}",
                        "worktree": "{{ fix.worktree }}"},
               "depends_on": ["fix"]}
    check = {"id": "check", "skip_if": "{{ not preview.pushed }}", "call": {"workflow": "review-branch"},
             "with": {"repo": "{{ repo }}", "slug": "{{ slug }}", "number": "{{ number }}", "ref": PREVIEW,
                      "base": "{{ base }}", "round": 2},
             "depends_on": ["preview"]}
    # The commit the pull request gets is exactly the one the branch review approved, never a later one.
    push["skip_if"] = ("{{ not preview.pushed or check.verdict != 'approve' or check.risk not in ['low', 'medium'] "
                       "or not fix.fixed or check.head != fix.commit }}")
    push["depends_on"] = ["check"]
    data = {
        "name": "pr-fix", "version": "1.0",
        "description": "Apply Armature's latest review when someone with write access comments /armature fix, review "
                       "the fix on a new branch, and push it to the pull request only when that review approves.",
        "contracts": {"inputs": [{"name": n, "type": t, "required": True} for n, t in
                                 (("repo", "string"), ("slug", "string"), ("number", "integer"), ("ref", "string"),
                                  ("branch", "string"), ("base", "string"), ("requester", "string"))],
                      "max_llm_calls": 50, "timeout_hours": 6},
        "stages": [
            {"id": "permission", "adapter": "github/permission",
             "with": {"repo": "{{ slug }}", "user": "{{ requester }}"}, "depends_on": []},
            {"id": "refused", "skip_if": "{{ permission.allowed }}", "adapter": "github/pr-comment",
             "with": {"repo": "{{ slug }}", "number": "{{ number }}",
                      "body": "Only someone with write access to this repository can run /armature fix, so nothing "
                              "was changed."},
             "depends_on": ["permission"]},
            {"id": "first", "skip_if": "{{ not permission.allowed }}", "call": {"workflow": "review-pass"},
             "with": {"repo": "{{ repo }}", "slug": "{{ slug }}", "number": "{{ number }}", "ref": "{{ ref }}",
                      "round": 1},
             "depends_on": ["permission"]},
            pushable, fix, preview, check, push, fixed, second],
    }
    return _dump(data)


CONFIG = {
    "events": ["pull_request_review_comment"],
    "conditions": [{"path": "action", "op": "eq", "value": "created"},
                   {"path": "comment.body", "op": "starts_with", "value": COMMAND, "ignore_case": True},
                   {"path": "sender.type", "op": "ne", "value": "Bot"}],
    "project_from": "repository.full_name",
    "key": ["repository.full_name", "pull_request.number"],
    "concurrency": "queue",
    "inputs": {"repo": "$.repository.clone_url", "slug": "$.repository.full_name",
               "number": "$.pull_request.number", "ref": "$.pull_request.head.sha",
               "branch": "$.pull_request.head.ref", "base": "$.pull_request.base.ref",
               "requester": "$.comment.user.login"},
}


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    callees = (("review-pass", starters.KINDS["review-pass"], starters.body("review-pass")),
               ("review-branch", "review", review_branch()))
    return Rows(name="pr-fix", kind="fix", body=body(), callees=callees, review_roles=True,
                rules=(Rule(WEBHOOK, CONFIG),), notes=(NEXT,))
