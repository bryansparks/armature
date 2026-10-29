"""The ops prompts, missions, and principles the remaining workflows need, as document rows.

The files under ``documents/`` are copies that tests/fixtures/ops/port_documents.py
made from armature-ops, and ``MANIFEST.json`` names each one's source. The
review roles and the PR team lead come from S14b's starter roles, so a
workflow names those keys here and never carries a copy.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from armature.server.migrate_ops import Document

DIR = Path(__file__).parent / "documents"
REVIEW_ROLES = ("simplicity", "correctness", "security", "performance", "beauty", "test_adequacy", "concurrency",
                "conventions")
STARTER_ROLES = (*REVIEW_ROLES, "compatibility", "team_lead")

NEEDS: dict[str, tuple[str, ...]] = {
    "slack-intake": ("mission/slack-intake", "prompt/slack_intake_classify", "prompt/slack_intake_principles"),
    "repo-review": ("mission/repo-review", "prompt/repo_review_principles", "prompt/_shared",
                    "prompt/repo_review_team_lead", "prompt/repo_review_digest", "prompt/repo_review_director"),
    "code-review": ("mission/code-review", "prompt/incremental_pr"),
    "pr-fix": ("prompt/pr_fix", "prompt/pr_fix_merge"),
    "development": ("mission/development", "prompt/development_principles", "prompt/development_platform",
                    "prompt/_shared_jira_consult", "prompt/_shared_jira_review", "prompt/development_brief",
                    "prompt/jira_work", "prompt/jira_work_backend", "prompt/jira_work_aws",
                    "prompt/jira_work_aws_backend", "prompt/jira_work_aws_ios"),
    "ios-port": ("mission/ios-port", "prompt/ios_port_principles", "prompt/ios_port", "prompt/ios_port_aws",
                 "prompt/ios_port_decide"),
    "localization": ("prompt/localization_translate",),
}

# The starter roles each workflow's agent stages run as. compatibility reviews only where ops' roles.json listed it.
ROLES: dict[str, tuple[str, ...]] = {
    "slack-intake": (),
    "repo-review": REVIEW_ROLES,
    "code-review": (*REVIEW_ROLES, "compatibility", "team_lead"),
    "pr-fix": (*REVIEW_ROLES, "compatibility", "team_lead"),
    "development": (*REVIEW_ROLES, "compatibility", "team_lead"),
    "ios-port": ("beauty", "conventions", "simplicity", "correctness"),
    "localization": (),
}


@cache
def manifest() -> dict[str, dict[str, str]]:
    return json.loads((DIR / "MANIFEST.json").read_text())


def documents(workflow: str) -> tuple[Document, ...]:
    """The documents ``workflow`` needs, on the team, since every project's copy of it shares them."""
    out = []
    for ref in NEEDS[workflow]:
        kind, key = ref.split("/", 1)
        out.append(Document(kind, key, (DIR / kind / f"{key}.md").read_text(), team=True))
    return tuple(out)
