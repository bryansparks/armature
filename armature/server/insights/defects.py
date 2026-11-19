"""Escaped-defect candidates: a merged fix linked to the merged pull requests that last touched its files before it.

These are candidates for a lead to confirm, never findings. The links come from stored file lists, so they are coarse
until blame-based attribution, which needs a clone, replaces them.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, exists, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server.db import new_id
from armature.server.db.schema import (
    gh_pr_commits,
    gh_pr_files,
    gh_pr_links,
    gh_pull_requests,
    labels,
)
from armature.server.insights import labeling
from armature.server.insights.collect import _found_or_added
from armature.server.insights.metrics import CHANGE_KIND

DETECTOR_VERSION = "1"
WINDOW = timedelta(days=90)
LINKS_MAX = 3
FILES_MAX = 100
TITLE_CONFIDENCE = 0.4
JIRA_CONFIDENCE = 0.7
OVERLAP_MIN = 2
FIX_TITLE = re.compile(r"^\s*(?:bug\s?)?fix(?:es|ed)?\b", re.IGNORECASE)
# Fixes to spelling, linting, or CI correct no earlier pull request's behavior.
NOT_DEFECTS = re.compile(r"\b(?:typos?|lint(?:s|ing|er)?|ci)\b", re.IGNORECASE)
# Files every change touches, which say nothing about whose code a fix corrects.
UNCOUNTED = frozenset({"package-lock.json", "yarn.lock", "Podfile.lock", "Package.resolved", "gradle.lockfile"})


def path_key(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def add_files(conn: Connection, team_id: str, repo_row_id: str, pr_row_id: str, paths: list[str],
              now: datetime) -> None:
    """Record the files a merged pull request changed. Stored ones stay, so a shorter list never removes any."""
    wanted = {path_key(p): p for p in paths[:FILES_MAX]}
    stored = set(conn.execute(select(gh_pr_files.c.path_key).where(gh_pr_files.c.pr_row_id == pr_row_id)).scalars())
    rows = [{"id": new_id(), "team_id": team_id, "repo_row_id": repo_row_id, "pr_row_id": pr_row_id, "path": path,
             "path_key": key, "created_at": now} for key, path in sorted(wanted.items()) if key not in stored]
    if not rows:
        return
    try:
        with conn.begin_nested():
            conn.execute(insert(gh_pr_files), rows)
    except IntegrityError:
        for row in rows:
            _found_or_added(conn, gh_pr_files, {"pr_row_id": pr_row_id, "path_key": row["path_key"]},
                            {k: v for k, v in row.items() if k not in ("id", "pr_row_id", "path_key")})


def is_fix(title: Any) -> bool:
    return isinstance(title, str) and FIX_TITLE.match(title) is not None and NOT_DEFECTS.search(title) is None


def counts(path: str) -> bool:
    """Whether a changed file is evidence: not a lock file, a changelog, a project file, or generated code."""
    name = path.rsplit("/", 1)[-1]
    return not (name in UNCOUNTED or name.upper().startswith("CHANGELOG") or name.endswith((".pbxproj", ".min.js"))
                or "/generated/" in f"/{path}")


def jira_bugs(conn: Connection, team_id: str, pr_row_ids: list[str]) -> set[str]:
    """The pull requests among ``pr_row_ids`` whose Jira issue type makes them a bug fix."""
    return set(conn.execute(select(labels.c.subject_id).where(
        labels.c.team_id == team_id, labels.c.subject_kind == CHANGE_KIND,
        labels.c.taxonomy_version == labeling.CHANGE_VERSION, labels.c.label == labeling.BUG_FIX,
        labels.c.labeler.startswith(labeling.JIRA, autoescape=True), labels.c.subject_id.in_(pr_row_ids))).scalars())


def link_fixes(conn: Connection, team_id: str, pr_row_ids: list[str], now: datetime) -> int:
    """Link each merged fix among ``pr_row_ids`` to up to LINKS_MAX merged pull requests that last touched at least
    OVERLAP_MIN of its files in the WINDOW before it merged. Returns how many links are new.

    A fix by its Jira issue type links at JIRA_CONFIDENCE, else a fix by its title at TITLE_CONFIDENCE. A fix that has
    links keeps them, so rerunning never adds past the cap, and a Jira bug raises their confidence.
    """
    made = 0
    bugs = jira_bugs(conn, team_id, pr_row_ids)
    for fix in conn.execute(select(gh_pull_requests).where(gh_pull_requests.c.id.in_(pr_row_ids),
                                                           gh_pull_requests.c.team_id == team_id,
                                                           gh_pull_requests.c.state == "merged",
                                                           gh_pull_requests.c.merged_at_gh.is_not(None))).all():
        confidence = JIRA_CONFIDENCE if fix.id in bugs else TITLE_CONFIDENCE
        if fix.id not in bugs and not is_fix(fix.title):
            continue
        if conn.execute(select(gh_pr_links.c.id).where(gh_pr_links.c.from_pr_row_id == fix.id,
                                                       gh_pr_links.c.kind == "fix").limit(1)).first() is not None:
            conn.execute(update(gh_pr_links).where(gh_pr_links.c.from_pr_row_id == fix.id, gh_pr_links.c.kind == "fix",
                                                   gh_pr_links.c.confidence < confidence).values(confidence=confidence))
            continue
        keys = [r.path_key for r in conn.execute(select(gh_pr_files.c.path_key, gh_pr_files.c.path).where(
            gh_pr_files.c.pr_row_id == fix.id)) if counts(r.path)]
        if len(keys) < OVERLAP_MIN:
            continue
        landed = exists().where(gh_pr_commits.c.pr_row_id == gh_pull_requests.c.id)
        touched = conn.execute(
            select(gh_pr_files.c.path_key, gh_pull_requests.c.id, gh_pull_requests.c.merged_at_gh)
            .join(gh_pull_requests, gh_pull_requests.c.id == gh_pr_files.c.pr_row_id)
            .where(gh_pr_files.c.team_id == team_id, gh_pr_files.c.repo_row_id == fix.repo_row_id,
                   gh_pr_files.c.path_key.in_(keys), gh_pull_requests.c.id != fix.id,
                   gh_pull_requests.c.state == "merged", gh_pull_requests.c.merged_at_gh < fix.merged_at_gh,
                   gh_pull_requests.c.merged_at_gh >= fix.merged_at_gh - WINDOW, landed)).all()
        last: dict[str, Any] = {}
        for row in touched:
            if row.path_key not in last or (row.merged_at_gh, row.id) > (last[row.path_key].merged_at_gh,
                                                                           last[row.path_key].id):
                last[row.path_key] = row
        files = Counter(row.id for row in last.values())
        merged = {row.id: row.merged_at_gh for row in last.values()}
        ranked = sorted((pr for pr in files if files[pr] >= OVERLAP_MIN),
                        key=lambda pr: (-files[pr], -merged[pr].timestamp(), pr))
        for target in ranked[:LINKS_MAX]:
            made += _found_or_added(conn, gh_pr_links, {"from_pr_row_id": fix.id, "to_pr_row_id": target,
                                                        "kind": "fix"},
                                    {"team_id": team_id, "evidence": {"title": (fix.title or "")[:200],
                                                                      "files": files[target]},
                                     "confidence": confidence, "detector_version": DETECTOR_VERSION,
                                     "created_at": now})[1]
    return made
