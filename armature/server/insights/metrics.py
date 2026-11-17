"""The numbers behind the Insights screens, read from a team's stored GitHub history, findings, and model calls.

Every function is read-only and team-scoped, and returns plain dicts. A Team loads the rows one screen needs once,
indexes them by time and by person, and the measures run over it in memory. Every measure comes with its value over
the equal-length window before, and the client words the change. Callers validate days, repos, and people first.
"""
from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import Connection, String, and_, case, cast, func, or_, select, union

from armature.server.db.schema import (
    candidate_matches,
    connections,
    eval_experiments,
    eval_judgments,
    finding_candidates,
    findings,
    fix_proposals,
    gh_check_runs,
    gh_conflict_samples,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    labels,
    llm_calls,
    metadata,
    people,
    person_identities,
    projects,
    runs,
    tool_calls,
    users,
)
from armature.server.insights import arms, fixes, scoring, taxonomy
from armature.server.insights import candidates as matcher
from armature.server.insights.attribution import ARMATURE
from armature.server.insights.labeling import COMMENT_VERSION

DAYS = (7, 30, 90)
TRAILING = timedelta(days=7)
WEEKS = 12
TREND_WEEKS = 13
WAITING_TOO_LONG = timedelta(days=2)
FAILED = frozenset({"failure", "timed_out", "startup_failure"})
KEPT = frozenset({"block", "suggest"})
TRACED = 0.6
# Lines changed below each bound, and the weight a merged pull request of that size carries. XL weighs 8.
SIZES = ((10, "XS", 1), (30, "S", 2), (100, "M", 3), (500, "L", 5))
XL = ("XL", 8)
REVIEW_CAP_H = 3.0
HOURS_PER_500_LINES = 0.5
# Phase 3c labels pull requests with a change type and review comments as substantive or a nit, under these kinds.
CHANGE_KIND = "pull_request"
CHANGE_TYPES = ("bug-fix", "enhancement", "new-feature", "code-maintenance", "platform-maintenance")
PLATFORM = "platform-maintenance"
COMMENT_KIND = "review_comment"
SUBSTANTIVE = "substantive"
TARGET_TOOLS = ("review.decisions", "github.post-review", "review.record-post")
POST_TOOLS = ("github.post-review", "review.record-post")
# A comparison run's tool calls, which never count as a review of their pull request.
ARM_MODE = "arm"
HOTSPOTS = 10
ATTENTION_MAX = 6
DROPPED_FLAG_ITEMS = 2
WAITING_PRS = 2
ROUNDS_RISE = 1.25
CONFLICT_LEAD = 2
DROPPED_BUGS_MAX = 20
GITHUB = "https://github.com"
IN_CHUNK = 500
VIEWS = ("shipping", "quality", "review", "unblocking")
FIX_STATUSES = ("waiting", "in_progress", "done", "declined")

# What each screen reads, so a Team loads no table it won't use.
CHILDREN = frozenset({"reviews", "comments", "pushes", "checks", "samples", "candidates", "posted", "files",
                      "labels"})
FOR_OVERVIEW = frozenset({"reviews", "comments", "samples", "candidates", "posted", "files", "labels"})
FOR_REPO = frozenset({"reviews", "comments", "samples", "checks", "pushes", "candidates", "posted", "files"})
FOR_PEOPLE = FOR_OVERVIEW
FOR_FINDINGS = frozenset({"candidates", "files"})


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def previous(self) -> Window:
        return Window(self.start - (self.end - self.start), self.start)

    def __contains__(self, at: datetime | None) -> bool:
        return at is not None and self.start <= at < self.end


def window(now: datetime, days: int) -> Window:
    return Window(now - timedelta(days=days), now)


def _median(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return statistics.median(kept) if kept else None


def _mean(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return statistics.fmean(kept) if kept else None


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _pair(measure: Callable[[Window], float | None], w: Window, digits: int = 2) -> dict[str, Any]:
    return {"value": _r(measure(w), digits), "previous": _r(measure(w.previous()), digits)}


def _chunks(values: list[Any]) -> Iterable[list[Any]]:
    for i in range(0, len(values), IN_CHUNK):
        yield values[i:i + IN_CHUNK]


def size_of(additions: int | None, deletions: int | None) -> tuple[str, int] | None:
    """The size bucket and weight of a change, or None when GitHub hasn't said how big it is."""
    if additions is None and deletions is None:
        return None
    lines = (additions or 0) + (deletions or 0)
    return next(((name, weight) for bound, name, weight in SIZES if lines < bound), XL)


def web_base(api_base: Any) -> str:
    """GitHub's web address for an API base: api.github.com is github.com, and https://host/api/v3 is https://host."""
    if not isinstance(api_base, str) or not api_base.strip():
        return GITHUB
    parts = urlsplit(api_base.strip().rstrip("/"))
    if not parts.scheme or not parts.netloc:
        return GITHUB
    if parts.netloc.lower() == "api.github.com":
        return GITHUB
    return f"{parts.scheme}://{parts.netloc}"


def _segments(value: str) -> str:
    """Each segment percent-encoded as encodeURIComponent would, keeping the slashes. Empty, ".", and ".."
    segments are dropped, because a browser resolves even an encoded ".." and would leave the repository."""
    kept = (part for part in value.split("/") if part and unquote(part) not in (".", ".."))
    return "/".join(quote(part, safe="!~*'()") for part in kept)


def blob_url(base: str, full_name: str, ref: str | None, path: str) -> str:
    return f"{base}/{_segments(full_name)}/blob/{_segments(ref or '') or 'HEAD'}/{_segments(path)}"


def line_url(base: str, full_name: str, head: str | None, path: str, line: int | None) -> str:
    return blob_url(base, full_name, head, path) + (f"#L{line}" if line else "")


def _overlap(a: datetime, b: datetime, w: Window) -> float:
    return max(0.0, _hours(min(b, w.end) - max(a, w.start)))


@dataclass
class PR:
    row: Any
    repo: Any
    web: str = GITHUB
    reviews: list[Any] = field(default_factory=list)
    comments: list[Any] = field(default_factory=list)
    pushes: list[Any] = field(default_factory=list)
    checks: list[Any] = field(default_factory=list)
    samples: list[Any] = field(default_factory=list)
    candidates: list[Any] = field(default_factory=list)
    posted_heads: set[str] = field(default_factory=set)
    files: set[str] | None = None
    change_type: str | None = None
    human_reviews: list[Any] = field(default_factory=list)
    heads: set[str] = field(default_factory=set)
    approval: datetime | None = None
    intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    id: str = ""

    def __post_init__(self) -> None:
        self.id = self.row.id

    @property
    def target(self) -> str:
        return f"{self.repo.full_name}#{self.row.number}".lower()

    @property
    def lines(self) -> int | None:
        if self.row.additions is None and self.row.deletions is None:
            return None
        return (self.row.additions or 0) + (self.row.deletions or 0)

    @property
    def first_review(self) -> datetime | None:
        return self.human_reviews[0].submitted_at if self.human_reviews else None

    @property
    def rounds(self) -> int:
        return len(self.heads)

    def conflict_hours(self, w: Window) -> float:
        return sum(_overlap(a, b, w) for a, b in self.intervals)

    def paths(self) -> set[str]:
        """The files this pull request changed, else the files its comments and flags name."""
        if self.files is not None:
            return self.files
        return {c.path for c in self.comments if c.path} | {c.row.path for c in self.candidates if c.row.path}

    def url(self) -> str:
        return f"{self.web}/{self.repo.full_name}/pull/{self.row.number}"


def intervals(samples: list[Any], until: datetime) -> list[tuple[datetime, datetime]]:
    """The spans a pull request was in conflict. Samples are stored only when the state changes. Unknown means
    GitHub is still recomputing, so a conflict lasts across it until a mergeable sample, or until ``until`` (its
    close, or now while it is open)."""
    spans: list[tuple[datetime, datetime]] = []
    began: datetime | None = None
    for sample in sorted(samples, key=lambda s: s.sampled_at):
        if sample.sampled_at >= until:
            break
        if sample.mergeable == "conflicting" and began is None:
            began = sample.sampled_at
        elif sample.mergeable == "mergeable" and began is not None:
            spans.append((began, sample.sampled_at))
            began = None
    if began is not None:
        spans.append((began, until))
    return spans


def run_targets(conn: Connection, team_id: str, run_ids: Any) -> dict[str, str]:
    """The pull request (owner/name#number, lowercased) each run reviewed: the target its review calls named, else
    its subject."""
    targets: dict[str, str] = {}
    for row in conn.execute(select(runs.c.id, runs.c.subject).where(runs.c.team_id == team_id,
                                                                    runs.c.id.in_(run_ids))):
        if row.subject and "#" in row.subject:
            targets[row.id] = row.subject.lower()
    for row in conn.execute(select(tool_calls.c.run_id, tool_calls.c.target).where(
            tool_calls.c.team_id == team_id, tool_calls.c.tool.in_(TARGET_TOOLS), tool_calls.c.mode != ARM_MODE,
            tool_calls.c.run_id.in_(run_ids)).order_by(tool_calls.c.created_at)):
        if row.target and "#" in row.target:
            targets[row.run_id] = row.target.lower()
    return targets


def newest_logins(conn: Connection, team_id: str) -> dict[str, str]:
    """Each person's GitHub login now: the github_login identity seen most recently."""
    found: dict[str, str] = {}
    for row in conn.execute(select(person_identities.c.person_id, person_identities.c.value).where(
            person_identities.c.team_id == team_id, person_identities.c.kind == "github_login")
            .order_by(person_identities.c.last_seen, person_identities.c.id)):
        found[row.person_id] = row.value
    return found


def file_table() -> Any:
    """Phase 2b's per-pull-request file list, when this build has one."""
    return metadata.tables.get("gh_pr_files")


def _web_bases(conn: Connection, team_id: str, repos: dict[str, Any]) -> dict[str, str]:
    """Each repo's GitHub web address: its own connection's, else the one the team's GitHub connections agree on."""
    bases = {r.id: web_base((r.config or {}).get("api_base")) for r in conn.execute(
        select(connections.c.id, connections.c.config).where(connections.c.team_id == team_id,
                                                             connections.c.kind == "github_app"))}
    shared = next(iter(set(bases.values()))) if len(set(bases.values())) == 1 else GITHUB
    return {repo_id: bases.get(repo.connection_id, shared) for repo_id, repo in repos.items()}


class _Timeline:
    """Items sorted by a timestamp, sliced by window with bisect."""

    def __init__(self, items: Iterable[Any], when: Callable[[Any], datetime | None]):
        dated = sorted(((when(i), i) for i in items if when(i) is not None), key=lambda pair: pair[0])
        self.times = [t for t, _ in dated]
        self.items = [i for _, i in dated]

    def within(self, w: Window) -> list[Any]:
        return self.items[bisect_left(self.times, w.start):bisect_left(self.times, w.end)]


class Team:
    """One team's pull requests touching [since, end), with the children ``children`` names, the links, and the
    findings raised on them. ``pr_id`` loads one pull request whatever its age."""

    def __init__(self, conn: Connection, team_id: str, since: datetime, end: datetime, *, repo_id: str | None = None,
                 pr_id: str | None = None, children: frozenset[str] = CHILDREN):
        self.team_id, self.since, self.end, self.children = team_id, since, end, children
        self.repos = {r.id: r for r in conn.execute(select(gh_repos).where(gh_repos.c.team_id == team_id))}
        self.web = _web_bases(conn, team_id, self.repos)
        self.repo_by_name = {r.full_name.lower(): r.id for r in self.repos.values()}
        self.people = {p.id: p for p in conn.execute(select(people).where(people.c.team_id == team_id))}
        self.logins = newest_logins(conn, team_id)
        self.armature = conn.execute(select(person_identities.c.person_id).where(
            person_identities.c.team_id == team_id, person_identities.c.kind == ARMATURE,
            person_identities.c.value == ARMATURE)).scalar()
        p = gh_pull_requests.c
        where = [p.team_id == team_id]
        if pr_id is not None:
            where.append(p.id == pr_id)
        else:
            where.append(or_(p.state == "open", p.created_at_gh >= since, p.closed_at_gh >= since,
                             p.merged_at_gh >= since))
        if repo_id is not None:
            where.append(p.repo_row_id == repo_id)
        ids = select(p.id).where(*where)
        self.prs = {r.id: self._pr(r) for r in conn.execute(select(gh_pull_requests).where(*where))}
        self.links = conn.execute(select(gh_pr_links).where(
            gh_pr_links.c.team_id == team_id,
            or_(gh_pr_links.c.from_pr_row_id.in_(ids), gh_pr_links.c.to_pr_row_id.in_(ids)))
            .order_by(gh_pr_links.c.created_at, gh_pr_links.c.id)).all()
        self.links_to: dict[str, list[Any]] = defaultdict(list)
        for link in self.links:
            self.links_to[link.to_pr_row_id].append(link)
        linked = {x for link in self.links for x in (link.from_pr_row_id, link.to_pr_row_id)} - set(self.prs)
        self.others = {r.id: self._pr(r) for r in conn.execute(
            select(gh_pull_requests).where(p.team_id == team_id, p.id.in_(linked)))} if linked else {}
        self.by_target = {pr.target: pr for pr in (*self.others.values(), *self.prs.values())}
        self._loaded = or_(and_(*where), p.id.in_(linked)) if linked else and_(*where)
        self._children(conn, ids, where, detail=pr_id is not None)
        self.comment_labels: dict[str, str] = {}
        self.candidates: list[Candidate] = []
        if "posted" in children:
            self._posted(conn)
        if "labels" in children:
            self._labels(conn, ids)
        if "files" in children:
            self._files(conn)
        if "candidates" in children:
            self._candidates(conn, pr_id)
        for pr in self.prs.values():
            self._derive(pr)
        self._index()

    def _pr(self, row: Any) -> PR:
        return PR(row, self.repos[row.repo_row_id], self.web[row.repo_row_id])

    def _children(self, conn: Connection, ids: Any, where: list[Any], *, detail: bool) -> None:
        loads = [(table, attr, extra) for table, attr, extra in (
            (gh_reviews, "reviews", ()), (gh_pushes, "pushes", ()), (gh_conflict_samples, "samples", ()),
            (gh_review_comments, "comments", (gh_review_comments.c.deleted.is_(False),)))
            if attr in self.children]
        if detail and "checks" in self.children:
            loads.append((gh_check_runs, "checks", ()))
        for table, attr, extra in loads:
            for row in conn.execute(select(table).where(table.c.team_id == self.team_id,
                                                        table.c.pr_row_id.in_(ids), *extra)):
                getattr(self.prs[row.pr_row_id], attr).append(row)
        if "checks" not in self.children:
            return
        ch, p = gh_check_runs.c, gh_pull_requests.c
        # Check runs on each pull request's final head, linked to it or not, by the (repo, head) index.
        linked = or_(ch.pr_row_id.is_(None), ch.pr_row_id == p.id) if not detail else ch.pr_row_id.is_(None)
        for row in conn.execute(select(gh_check_runs, p.id.label("for_pr")).select_from(
                gh_check_runs.join(gh_pull_requests, and_(p.repo_row_id == ch.repo_row_id,
                                                          p.head_sha == ch.head_sha)))
                .where(ch.team_id == self.team_id, linked, *where)):
            self.prs[row.for_pr].checks.append(row)

    def _posted(self, conn: Connection) -> None:
        targets = sorted(pr.target for pr in self.prs.values())
        for chunk in _chunks(targets):
            for row in conn.execute(select(tool_calls.c.target, tool_calls.c.detail).where(
                    tool_calls.c.tool.in_(POST_TOOLS), tool_calls.c.target.in_(chunk),
                    tool_calls.c.team_id == self.team_id, tool_calls.c.mode == "live")):
                pr = self.by_target.get((row.target or "").lower())
                detail = row.detail if isinstance(row.detail, dict) else {}
                if pr is not None and detail.get("posted") is True and isinstance(detail.get("head"), str):
                    pr.posted_heads.add(detail["head"])

    def _labels(self, conn: Connection, ids: Any) -> None:
        latest: dict[str, Any] = {}
        for row in conn.execute(select(labels.c.subject_id, labels.c.label, labels.c.created_at).where(
                labels.c.team_id == self.team_id, labels.c.subject_kind == CHANGE_KIND, labels.c.subject_id.in_(ids),
                labels.c.label.in_(CHANGE_TYPES))):
            if row.subject_id not in latest or row.created_at > latest[row.subject_id].created_at:
                latest[row.subject_id] = row
        for pr_id, row in latest.items():
            self.prs[pr_id].change_type = row.label
        if "comments" not in self.children:
            return
        comment_ids = select(gh_review_comments.c.id).where(gh_review_comments.c.team_id == self.team_id,
                                                            gh_review_comments.c.pr_row_id.in_(ids))
        self.comment_labels = {r.subject_id: r.label for r in conn.execute(
            select(labels.c.subject_id, labels.c.label).where(
                labels.c.team_id == self.team_id, labels.c.subject_kind == COMMENT_KIND,
                labels.c.taxonomy_version == COMMENT_VERSION, labels.c.subject_id.in_(comment_ids)))}

    def _files(self, conn: Connection) -> None:
        table = file_table()
        every = {**self.others, **self.prs}
        if table is None or not every:
            return
        for chunk in _chunks(list(every)):
            for row in conn.execute(select(table.c.pr_row_id, table.c.path).where(table.c.team_id == self.team_id,
                                                                                  table.c.pr_row_id.in_(chunk))):
                pr = every[row.pr_row_id]
                pr.files = (pr.files or set()) | {row.path}

    def _runs_for(self, targets: Any) -> Any:
        return union(
            select(tool_calls.c.run_id).where(tool_calls.c.team_id == self.team_id, tool_calls.c.mode != ARM_MODE,
                                              tool_calls.c.tool.in_(TARGET_TOOLS), tool_calls.c.target.in_(targets)),
            select(runs.c.id).where(runs.c.team_id == self.team_id, func.lower(runs.c.subject).in_(targets)))

    def _candidates(self, conn: Connection, pr_id: str | None) -> None:
        fc, p, g = finding_candidates.c, gh_pull_requests.c, gh_repos.c
        # Every loaded pull request keeps all its flags, whenever they were raised, so a measure of a pull request
        # reads the same on every screen, and a flag dropped long ago can still turn into a bug.
        targets = select(func.lower(g.full_name + "#" + cast(p.number, String))).select_from(
            gh_pull_requests.join(gh_repos, g.id == p.repo_row_id)).where(self._loaded)
        reviewed = fc.run_id.in_(self._runs_for(targets))
        chosen = reviewed if pr_id is not None else or_(fc.created_at >= self.since, reviewed)
        where = [fc.team_id == self.team_id, fc.arm_id.is_(None), chosen]
        ids = select(fc.id).where(*where)
        run_ids = select(fc.run_id).where(*where)
        targets = run_targets(conn, self.team_id, run_ids)
        heads = {r.id: r.head_sha for r in conn.execute(select(runs.c.id, runs.c.head_sha).where(
            runs.c.team_id == self.team_id, runs.c.id.in_(run_ids)))}
        decided: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
        for row in conn.execute(select(candidate_matches.c.candidate_id, candidate_matches.c.method,
                                       findings.c.decision)
                                .select_from(candidate_matches.outerjoin(
                                    findings, findings.c.id == candidate_matches.c.finding_id))
                                .where(candidate_matches.c.team_id == self.team_id,
                                       candidate_matches.c.matcher_version == matcher.MATCHER_VERSION,
                                       candidate_matches.c.candidate_id.in_(ids))):
            decided[row.candidate_id].append((row.method, row.decision))
        named = {r.subject_id: r.label for r in conn.execute(select(labels.c.subject_id, labels.c.label).where(
            labels.c.team_id == self.team_id, labels.c.subject_kind == "candidate",
            labels.c.taxonomy_version == taxonomy.VERSION, labels.c.subject_id.in_(ids)))}
        for row in conn.execute(select(fc.id, fc.run_id, fc.specialist, fc.ordinal, fc.path, fc.line, fc.claim,
                                       fc.severity, fc.created_at).where(*where)
                                .order_by(fc.created_at, fc.specialist, fc.ordinal, fc.id)):
            target = targets.get(row.run_id)
            pr = self.by_target.get(target) if target else None
            repo_id = pr.repo.id if pr else self.repo_by_name.get((target or "").rpartition("#")[0])
            item = Candidate(row=row, target=target, pr=pr, repo_id=repo_id, head=heads.get(row.run_id),
                             label=named.get(row.id), status=_status(decided.get(row.id)))
            self.candidates.append(item)
            if pr is not None:
                pr.candidates.append(item)

    def by_target_of(self, pr_id: str) -> str | None:
        pr = self.prs.get(pr_id) or self.others.get(pr_id)
        return pr.target if pr else None

    def pr(self, pr_id: str) -> PR | None:
        return self.prs.get(pr_id) or self.others.get(pr_id)

    def human(self, person_id: str | None, login: str | None) -> bool:
        person = self.people.get(person_id) if person_id else None
        if person is not None:
            return person.kind == "human" and person.id != self.armature
        return not (login or "").lower().endswith("[bot]")

    def _derive(self, pr: PR) -> None:
        submitted = sorted((r for r in pr.reviews if r.state != "pending" and r.submitted_at is not None),
                           key=lambda r: r.submitted_at)
        pr.human_reviews = [r for r in submitted if r.actor_source == "github"
                            and r.reviewer_gh_id != pr.row.author_gh_id
                            and self.human(r.reviewer_person_id, r.reviewer_login)]
        armature = [r for r in submitted if r.actor_source == ARMATURE]
        pr.heads = {r.head_sha for r in (*pr.human_reviews, *armature) if r.head_sha} | pr.posted_heads
        merged = pr.row.merged_at_gh
        approvals = [r.submitted_at for r in pr.human_reviews if r.state == "approved"
                     and (merged is None or r.submitted_at <= merged)]
        pr.approval = approvals[-1] if approvals else None
        pr.intervals = intervals(pr.samples, min(pr.row.closed_at_gh or pr.row.merged_at_gh or self.end, self.end))

    def _index(self) -> None:
        prs = list(self.prs.values())
        self._merged = {None: _Timeline(prs, lambda pr: pr.row.merged_at_gh)}
        self._opened = {None: _Timeline(prs, lambda pr: pr.row.created_at_gh)}
        self._first_reviewed = {None: _Timeline(prs, lambda pr: pr.first_review)}
        for repo_id in self.repos:
            mine = [pr for pr in prs if pr.repo.id == repo_id]
            self._merged[repo_id] = _Timeline(mine, lambda pr: pr.row.merged_at_gh)
            self._opened[repo_id] = _Timeline(mine, lambda pr: pr.row.created_at_gh)
            self._first_reviewed[repo_id] = _Timeline(mine, lambda pr: pr.first_review)
        self.open_prs = [pr for pr in prs if pr.row.state == "open"]
        self.conflicted = [pr for pr in prs if pr.intervals]
        self.by_author: dict[str, list[PR]] = defaultdict(list)
        for pr in prs:
            self.by_author[pr.row.author_person_id].append(pr)
        self.own: dict[str, set[str]] = defaultdict(set)
        for pr in (*prs, *self.others.values()):
            self.own[pr.row.author_person_id].add(pr.id)
        given = [(pr, r) for pr in prs for r in pr.human_reviews]
        self.reviews = _Timeline(given, lambda pair: pair[1].submitted_at)
        self.reviews_by: dict[str, list[tuple[PR, Any]]] = defaultdict(list)
        for pr, r in self.reviews.items:
            self.reviews_by[r.reviewer_person_id].append((pr, r))
        self.comments_by: dict[str, list[tuple[PR, Any]]] = defaultdict(list)
        roots = []
        for pr in prs:
            # Armature posts through a person's own gh CLI login, so its review's comments are never theirs.
            armatures = {r.gh_review_id for r in pr.reviews if r.actor_source == ARMATURE}
            for c in pr.comments:
                if c.gh_review_id is not None and c.gh_review_id in armatures:
                    continue
                self.comments_by[c.author_person_id].append((pr, c))
                if (c.path and c.in_reply_to is None and c.author_gh_id != pr.row.author_gh_id
                        and self.human(c.author_person_id, c.author_login)):
                    roots.append((pr, c))
        self.root_comments = _Timeline(roots, lambda pair: pair[1].created_at_gh)
        self.merged_by = {person: _Timeline(mine, lambda pr: pr.row.merged_at_gh)
                          for person, mine in self.by_author.items()}
        # Each person's reviews and comments on others' pull requests, and their first review of each one.
        self.reviewed_by: dict[str, _Timeline] = {}
        self.firsts_by: dict[str, _Timeline] = {}
        for person, pairs in self.reviews_by.items():
            theirs = [(pr, r) for pr, r in pairs if pr.row.author_person_id != person]
            self.reviewed_by[person] = _Timeline(theirs, lambda pair: pair[1].submitted_at)
            first: dict[str, tuple[PR, datetime]] = {}
            for pr, r in theirs:
                first.setdefault(pr.id, (pr, r.submitted_at))
            self.firsts_by[person] = _Timeline(first.values(), lambda pair: pair[1])
        self.wrote_by: dict[str, _Timeline] = {}
        self.wrote_on: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for person, pairs in self.comments_by.items():
            theirs = [(pr, c) for pr, c in pairs if pr.row.author_person_id != person]
            self.wrote_by[person] = _Timeline(theirs, lambda pair: pair[1].created_at_gh)
            for pr, c in theirs:
                self.wrote_on[(person, pr.id)].append(c)
        self._flags = _Timeline(self.candidates, lambda c: c.row.created_at)

    def merged(self, w: Window, repo_id: str | None = None) -> list[PR]:
        found = self._merged.get(repo_id)
        return found.within(w) if found else []

    def opened(self, w: Window, repo_id: str | None = None) -> list[PR]:
        found = self._opened.get(repo_id)
        return found.within(w) if found else []

    def first_reviewed(self, w: Window, repo_id: str | None = None) -> list[PR]:
        found = self._first_reviewed.get(repo_id)
        return found.within(w) if found else []

    def flags(self, w: Window) -> list[Candidate]:
        return self._flags.within(w)

    def name(self, person_id: str | None) -> str | None:
        person = self.people.get(person_id) if person_id else None
        return person.display_name if person else None

    def pr_brief(self, pr: PR) -> dict[str, Any]:
        return {"id": pr.id, "repo_id": pr.repo.id, "repo": pr.repo.full_name, "number": pr.row.number,
                "title": pr.row.title, "author_id": pr.row.author_person_id,
                "author": self.name(pr.row.author_person_id) or pr.row.author_login,
                "author_login": self.logins.get(pr.row.author_person_id), "state": pr.row.state,
                "url": pr.url()}


@dataclass
class Candidate:
    row: Any
    target: str | None
    pr: PR | None
    repo_id: str | None
    head: str | None
    label: str | None
    status: str | None


def _status(decided: list[tuple[str, str | None]] | None) -> str | None:
    """kept when the lead blocked or suggested it, dropped when nothing took it or the lead dropped it, None before
    any decision."""
    if not decided:
        return None
    decisions = {decision for _, decision in decided}
    if decisions & KEPT:
        return "kept"
    if any(method == "none" for method, _ in decided) or "drop" in decisions:
        return "dropped"
    return "other"


# Measures. Each takes the loaded team and a window and returns a number, or None when nothing was measured.


def merged_count(team: Team, w: Window, repo_id: str | None = None) -> float:
    return len(team.merged(w, repo_id))


def time_to_merge_days(team: Team, w: Window, repo_id: str | None = None) -> float | None:
    return _median(_hours(pr.row.merged_at_gh - pr.row.created_at_gh) / 24 for pr in team.merged(w, repo_id))


def time_to_first_review_hours(team: Team, w: Window, repo_id: str | None = None) -> float | None:
    """Over pull requests whose first human review from someone other than the author came in the window, so a
    pull request still waiting never counts. Ready-for-review time isn't stored, so it runs from creation."""
    return _median(_hours(pr.first_review - pr.row.created_at_gh) for pr in team.first_reviewed(w, repo_id))


def review_rounds(team: Team, w: Window, repo_id: str | None = None) -> float | None:
    return _mean(pr.rounds for pr in team.merged(w, repo_id))


def conflict_hours(team: Team, w: Window, repo_id: str | None = None) -> float:
    return sum(pr.conflict_hours(w) for pr in team.conflicted if repo_id is None or pr.repo.id == repo_id)


def ci_failure_rate(team: Team, w: Window, repo_id: str | None = None) -> float | None:
    done = [c for pr in team.merged(w, repo_id) for c in pr.checks
            if c.head_sha == pr.row.head_sha and c.status == "completed"]
    return sum(c.conclusion in FAILED for c in done) / len(done) if done else None


OVERVIEW_METRICS = (("merged", merged_count, "count", 0), ("time_to_merge", time_to_merge_days, "days", 2),
                    ("review_rounds", review_rounds, "rounds", 2), ("conflict_hours", conflict_hours, "hours", 1))
REPO_TILES = (("time_to_first_review", time_to_first_review_hours, "hours", 1),
              ("review_rounds", review_rounds, "rounds", 2), ("conflict_hours", conflict_hours, "hours", 1),
              ("ci_failure_rate", ci_failure_rate, "share", 3))


def days_of(w: Window) -> list[datetime]:
    """The end of each day in the window. Each point of a daily series measures the 7 days before it."""
    count = round((w.end - w.start) / timedelta(days=1))
    return [w.start + timedelta(days=i + 1) for i in range(count)]


def daily(team: Team, w: Window, measure: Callable[..., float | None], repo_ids: Iterable[str],
          digits: int) -> dict[str, list[float | None]]:
    ends = days_of(w)
    return {repo_id: [_r(measure(team, Window(end - TRAILING, end), repo_id), digits) for end in ends]
            for repo_id in repo_ids}


def stages(team: Team, w: Window, repo_id: str) -> dict[str, Any]:
    """The hours the median merged pull request spent in each stage. Conflicts overlap the other stages."""
    merged = sorted(team.merged(w, repo_id), key=lambda pr: (pr.row.merged_at_gh - pr.row.created_at_gh,
                                                            pr.row.merged_at_gh, pr.id))
    if not merged:
        return {"pr": None, "waiting_first_review": None, "in_review": None, "blocked_by_conflicts": None,
                "waiting_on_ci": None, "approved_to_merged": None}
    pr = merged[(len(merged) - 1) // 2]
    return {"pr": team.pr_brief(pr), **pr_stages(pr)}


def pr_stages(pr: PR) -> dict[str, float | None]:
    """Hours from opening to the first review, from it to the last approval, in conflict, from the approval to the
    final head's last check, and from the approval to the merge. With no approval, the merge stands in for it."""
    created, merged_at = pr.row.created_at_gh, pr.row.merged_at_gh
    approved = pr.approval or merged_at
    # A comment after the merge isn't a review that held it up, and the stages never sum past the time to merge.
    first = pr.first_review if pr.first_review is not None and pr.first_review <= merged_at else None
    first = min(first, approved) if first is not None else None
    ci_done = [c.completed_at for c in pr.checks if c.head_sha == pr.row.head_sha and c.completed_at is not None]
    ci = _hours(max(ci_done) - approved) if ci_done else 0.0
    return {"waiting_first_review": _r(max(0.0, _hours((first or approved) - created))),
            "in_review": _r(max(0.0, _hours(approved - first))) if first else 0.0,
            "blocked_by_conflicts": _r(sum(_hours(b - a) for a, b in pr.intervals)),
            "waiting_on_ci": _r(max(0.0, ci)),
            "approved_to_merged": _r(max(0.0, _hours(merged_at - approved)))}


def push_kinds(team: Team, w: Window, repo_id: str | None = None) -> list[dict[str, Any]]:
    """Pushes by class. A push the classifier hasn't reached yet has kind None."""
    counts = Counter(p.push_class for pr in team.prs.values() if repo_id is None or pr.repo.id == repo_id
                     for p in pr.pushes if p.pushed_at in w)
    total = sum(counts.values())
    return [{"kind": kind, "count": n, "share": _r(n / total, 3)}
            for kind, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] or "~"))]


MEASURES = ("findings", "conflict_prs", "churn")


def hotspots(team: Team, w: Window, repo_id: str | None = None, limit: int | None = HOTSPOTS) -> list[dict[str, Any]]:
    """Files ranked by findings, conflict-touching pull requests, and churn, each ranked on its own, then by the
    sum of the three ranks. ``limit`` None returns every file."""
    found: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"findings": 0, "conflict_prs": set(), "conflict_hours": 0.0, "churn": set()})
    for c in team.flags(w):
        if c.row.path and c.repo_id and (repo_id is None or c.repo_id == repo_id):
            found[(c.repo_id, c.row.path)]["findings"] += 1
    for pr, comment in team.root_comments.within(w):
        if repo_id is None or pr.repo.id == repo_id:
            found[(pr.repo.id, comment.path)]["findings"] += 1
    for pr in team.conflicted:
        hours = pr.conflict_hours(w)
        if hours > 0 and pr.files and (repo_id is None or pr.repo.id == repo_id):
            for path in pr.files:
                entry = found[(pr.repo.id, path)]
                entry["conflict_prs"].add(pr.id)
                entry["conflict_hours"] += hours
    for pr in team.merged(w, repo_id):
        for path in pr.paths():
            found[(pr.repo.id, path)]["churn"].add(pr.id)
    rows = [{"repo_id": key[0], "repo": team.repos[key[0]].full_name, "path": key[1], "findings": v["findings"],
             "conflict_prs": len(v["conflict_prs"]), "conflict_hours": _r(v["conflict_hours"]),
             "churn": len(v["churn"])} for key, v in found.items()]
    ranks = {}
    for measure in MEASURES:
        ordered = sorted(row[measure] for row in rows)
        # A competition rank: one more than the number of files strictly above.
        ranks[measure] = [1 + len(ordered) - bisect_right(ordered, row[measure]) for row in rows]
    total = [sum(ranks[m][i] for m in MEASURES) for i in range(len(rows))]
    order = sorted(range(len(rows)), key=lambda i: (total[i], -rows[i]["findings"], -rows[i]["conflict_prs"],
                                                    -rows[i]["churn"], rows[i]["repo"], rows[i]["path"]))
    ranked = [rows[i] for i in order]
    return ranked if limit is None else ranked[:limit]


def _hot(spots: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(row["repo_id"], row["path"]) for row in spots[:HOTSPOTS]}


def _fixed_at(team: Team, link: Any) -> datetime:
    """When the revert or fix landed: its merge, else its opening, else when the link was found."""
    fixer = team.pr(link.from_pr_row_id)
    return (fixer.row.merged_at_gh or fixer.row.created_at_gh) if fixer else link.created_at


def _traced(team: Team, link: Any, w: Window) -> bool:
    when = _fixed_at(team, link)
    confirmed = isinstance(link.evidence, dict) and link.evidence.get("confirmed") is True
    return when in w and (confirmed or link.confidence >= TRACED)


def _review_hours(comments: list[Any], pr: PR, review: Any) -> float:
    started = [c.created_at_gh for c in comments if c.gh_review_id == review.gh_review_id
               and c.created_at_gh is not None]
    if started:
        return min(REVIEW_CAP_H, max(0.0, _hours(review.submitted_at - min(started))))
    return min(REVIEW_CAP_H, HOURS_PER_500_LINES * (pr.lines or 0) / 500)


def person_measures(team: Team, w: Window, person_id: str, hot: set[tuple[str, str]]) -> dict[str, Any]:
    """The four areas for one person, read from their own rows. Substantive comments stay None until comments are
    labeled."""
    nothing = _Timeline((), lambda _: None)
    shipped = team.merged_by.get(person_id, nothing).within(w)
    by_type = Counter(pr.change_type or "unclassified" for pr in shipped)
    sizes = [size_of(pr.row.additions, pr.row.deletions) for pr in shipped]
    rounds = sum(pr.rounds for pr in shipped)
    kept = sum(c.status == "kept" for pr in shipped for c in pr.candidates)
    traced = sum(_traced(team, link, w) for pr_id in team.own.get(person_id, ())
                 for link in team.links_to.get(pr_id, ()))
    given = team.reviewed_by.get(person_id, nothing).within(w)
    reviewed = {pr.id: pr for pr, _ in given}
    # Their earliest review of each pull request, counted only in the window it falls in. Request times aren't
    # stored, so it runs from the pull request's creation.
    firsts = team.firsts_by.get(person_id, nothing).within(w)
    recent = [c for _, c in team.wrote_by.get(person_id, nothing).within(w)]
    # A count is final only once every comment in it is labeled.
    substantive = sum(team.comment_labels[c.id] == SUBSTANTIVE for c in recent) \
        if all(c.id in team.comment_labels for c in recent) else None
    return {
        "shipping": {"merged": len(shipped), "by_type": dict(by_type),
                     "size_weighted": sum(s[1] for s in sizes if s is not None)},
        "quality": {"rounds_per_pr": _r(rounds / len(shipped)) if shipped else None,
                    "kept_per_round": _r(kept / rounds) if rounds else None, "bugs_traced": traced},
        "review": {"reviews_given": len(given), "lines_reviewed": sum(pr.lines or 0 for pr in reviewed.values()),
                   "first_review_hours": _r(_median(_hours(at - pr.row.created_at_gh) for pr, at in firsts)),
                   "substantive_comments": substantive,
                   "review_hours": _r(sum(_review_hours(team.wrote_on.get((person_id, pr.id), ()), pr, r)
                                          for pr, r in given))},
        "unblocking": {"hotspot_prs": sum(any((pr.repo.id, path) in hot for path in pr.paths()) for pr in shipped),
                       "platform_prs": sum(pr.change_type == PLATFORM for pr in shipped)},
    }


def _active(team: Team, w: Window) -> set[str]:
    found = {pr.row.author_person_id for pr in (*team.merged(w), *team.opened(w), *team.open_prs)}
    found.update(r.reviewer_person_id for _, r in team.reviews.within(w))
    return {p for p in found if p and p in team.people and team.human(p, None)}


def _main_repo(team: Team, person_id: str) -> str | None:
    counts = Counter(pr.repo.full_name for pr in team.by_author.get(person_id, ()))
    counts.update(pr.repo.full_name for pr, _ in team.reviews_by.get(person_id, ()))
    return min(counts, key=lambda name: (-counts[name], name)) if counts else None


def _team_median(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for area in VIEWS:
        out[area] = {}
        for key in rows[0][area] if rows else ():
            if key == "by_type":
                continue
            out[area][key] = _r(_median(row[area][key] for row in rows))
    return out


def people_rows(team: Team, w: Window, spots: list[dict[str, Any]] | None = None
                ) -> tuple[list[dict[str, Any]], dict[str, Any], set[tuple[str, str]]]:
    """A row per active human, the team median of every measure, and the hotspot files used. Bots and Armature
    never appear."""
    hot = _hot(spots if spots is not None else hotspots(team, w))
    rows = []
    for person_id in sorted(_active(team, w), key=lambda p: (team.people[p].display_name.lower(), p)):
        rows.append({"id": person_id, "name": team.people[person_id].display_name, "login": team.logins.get(person_id),
                     "repo": _main_repo(team, person_id), **person_measures(team, w, person_id, hot)})
    return rows, _team_median(rows), hot


def armature_reviews(team: Team, w: Window) -> int:
    return sum(1 for pr in team.prs.values() for r in pr.reviews
               if r.actor_source == ARMATURE and r.submitted_at in w)


# Findings


def _filtered(team: Team, w: Window, repo_id: str | None, category: str | None,
              person_id: str | None) -> list[Candidate]:
    return [c for c in team.flags(w)
            if (repo_id is None or c.repo_id == repo_id) and (category is None or c.label == category)
            and (person_id is None or (c.pr is not None and c.pr.row.author_person_id == person_id))]


def _became_bug(team: Team, c: Candidate) -> list[Any]:
    """The revert and fix links that later point at a dropped flag's pull request, where the fix touched the
    flagged file when its file list is known."""
    if c.status != "dropped" or c.pr is None:
        return []
    found = []
    for link in team.links_to.get(c.pr.id, ()):
        fixer = team.pr(link.from_pr_row_id)
        if fixer is None or fixer.row.created_at_gh < c.row.created_at:
            continue
        if fixer.files is not None and c.row.path not in fixer.files:
            continue
        found.append(link)
    return found


def category_counts(team: Team, w: Window, repo_id: str | None, person_id: str | None) -> Counter:
    return Counter(c.label for c in _filtered(team, w, repo_id, None, person_id) if c.label)


def _flag(c: Candidate) -> dict[str, Any]:
    url = None
    if c.pr is not None and c.row.path:
        url = line_url(c.pr.web, c.pr.repo.full_name, c.head or c.pr.row.head_sha, c.row.path, c.row.line)
    return {"id": c.row.id, "specialist": c.row.specialist, "category": c.label, "path": c.row.path,
            "line": c.row.line, "claim": c.row.claim, "severity": c.row.severity, "status": c.status, "url": url,
            "at": _iso(c.row.created_at)}


def findings_view(team: Team, w: Window, *, repo_id: str | None = None, category: str | None = None,
                  person_id: str | None = None) -> dict[str, Any]:
    now = category_counts(team, w, repo_id, person_id)
    before = category_counts(team, w.previous(), repo_id, person_id)
    top = sorted(now, key=lambda k: (-now[k], k))[:10]
    weekly = None
    if category is not None:
        ends = [w.end - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]
        repos = sorted({c.repo_id for c in team.candidates if c.repo_id and (repo_id is None or c.repo_id == repo_id)})
        counts = {r: [0] * WEEKS for r in repos}
        for i, end in enumerate(ends):
            for c in _filtered(team, Window(end - timedelta(weeks=1), end), repo_id, category, person_id):
                if c.repo_id in counts:
                    counts[c.repo_id][i] += 1
        weekly = {"weeks": [_iso(end) for end in ends], "repos": counts}
    chosen = _filtered(team, w, repo_id, category, person_id)
    by_specialist: dict[str, list[Candidate]] = defaultdict(list)
    for c in chosen:
        by_specialist[c.row.specialist].append(c)
    specialists = []
    became = []
    for name, raised in by_specialist.items():
        bugs = [(c, link) for c in raised for link in _became_bug(team, c)]
        became += bugs
        specialists.append({"specialist": name, "raised": len(raised),
                            "kept_share": _r(sum(c.status == "kept" for c in raised) / len(raised), 3),
                            "dropped_share": _r(sum(c.status == "dropped" for c in raised) / len(raised), 3),
                            "dropped_then_bug": len({c.row.id for c, _ in bugs})})
    specialists.sort(key=lambda s: (-s["dropped_then_bug"], -s["raised"], s["specialist"]))
    became.sort(key=lambda pair: pair[0].row.created_at, reverse=True)
    per_repo: dict[str, Counter] = defaultdict(Counter)
    for c in _filtered(team, w, repo_id, None, person_id):
        if c.repo_id and c.label:
            per_repo[c.repo_id][c.label] += 1
    return {
        "categories": [{"category": k, "count": now[k], "previous": before.get(k, 0)} for k in top],
        "top_by_repo": [{"repo_id": r, "repo": team.repos[r].full_name,
                         "category": min(counts, key=lambda k: (-counts[k], k)),
                         "count": max(counts.values())}
                        for r, counts in sorted(per_repo.items(), key=lambda kv: team.repos[kv[0]].full_name)],
        "unlabeled": sum(1 for c in chosen if c.label is None),
        "weekly": weekly,
        "specialists": specialists,
        "dropped_became_bugs": [{"flag": _flag(c), "pr": team.pr_brief(c.pr), "kind": link.kind,
                                 "bug_pr": team.pr_brief(team.pr(link.from_pr_row_id)),
                                 "confidence": link.confidence} for c, link in became[:DROPPED_BUGS_MAX]],
    }


# Models


def _specialist(step_key: str | None) -> str:
    if not step_key:
        return "unknown"
    return step_key.rsplit("/", 1)[-1].split("#", 1)[0] or "unknown"


def _comparison_spend(conn: Connection, team_id: str, w: Window, wanted: Any, specialist: str | None,
                      ) -> dict[str, float]:
    """What comparison runs spent on model calls in each period, and what their judge spent."""
    prev, c, j = w.previous(), llm_calls.c, eval_judgments.c
    period = case((c.created_at >= w.start, "now"), else_="before").label("period")
    found = {"now": 0.0, "before": 0.0}
    for row in conn.execute(select(period, c.step_key, func.coalesce(func.sum(c.cost_usd), 0).label("spend"))
                            .where(c.team_id == team_id, c.cached.is_(False), c.created_at >= prev.start,
                                   c.created_at < w.end, c.run_id.in_(arms.arm_runs(team_id)))
                            .group_by(period, c.step_key)):
        if wanted(row.step_key):
            found[row.period] += float(row.spend)
    judged = case((j.created_at >= w.start, "now"), else_="before").label("period")
    query = (select(judged, func.coalesce(func.sum(j.cost_usd), 0).label("spend"))
             .where(j.team_id == team_id, j.created_at >= prev.start, j.created_at < w.end).group_by(judged))
    if specialist is not None:
        query = query.where(j.experiment_id.in_(select(eval_experiments.c.id).where(
            eval_experiments.c.team_id == team_id, eval_experiments.c.specialist == specialist)))
    for row in conn.execute(query):
        found[row.period] += float(row.spend)
    return found


def models_view(conn: Connection, team_id: str, w: Window, specialist: str | None = None) -> dict[str, Any]:
    """Model spend from recorded calls, not cached ones, summed in SQL, with comparison runs apart, and the
    comparisons and recommendations waiting for approval."""
    prev = w.previous()
    c = llm_calls.c
    period = case((c.created_at >= w.start, "now"), else_="before").label("period")
    spend = func.coalesce(func.sum(c.cost_usd), 0).label("spend")
    scoped = (c.team_id == team_id, c.cached.is_(False), c.created_at >= prev.start, c.created_at < w.end,
              arms.not_arm(c.run_id, team_id))
    wanted = (lambda key: True) if specialist is None else (lambda key: _specialist(key) == specialist)
    totals = {"now": 0.0, "before": 0.0}
    by_specialist = {"now": Counter(), "before": Counter()}
    by_model = {"now": Counter(), "before": Counter()}
    for row in conn.execute(select(period, c.step_key, c.model_id, spend).where(*scoped)
                            .group_by(period, c.step_key, c.model_id)):
        if wanted(row.step_key):
            cost = float(row.spend)
            totals[row.period] += cost
            by_specialist[row.period][_specialist(row.step_key)] += cost
            by_model[row.period][row.model_id or "unknown"] += cost
    per_run = [r for r in conn.execute(select(period, c.run_id, c.step_key, spend).where(*scoped)
                                       .group_by(period, c.run_id, c.step_key)) if wanted(r.step_key)]
    reviewed = run_targets(conn, team_id, sorted({r.run_id for r in per_run})) if per_run else {}
    per_pr: dict[str, float | None] = {}
    prs: dict[str, int] = {}
    for key in ("now", "before"):
        chosen = [r for r in per_run if r.period == key and r.run_id in reviewed]
        targets = {reviewed[r.run_id] for r in chosen}
        prs[key] = len(targets)
        per_pr[key] = sum(float(r.spend) for r in chosen) / len(targets) if targets else None

    def split(found: dict[str, Counter]) -> list[dict[str, Any]]:
        names = sorted(set(found["now"]) | set(found["before"]), key=lambda k: (-found["now"].get(k, 0), k))
        return [{"name": k, "spend_usd": _r(found["now"].get(k, 0.0), 4),
                 "previous": _r(found["before"].get(k, 0.0), 4)} for k in names]

    compared = _comparison_spend(conn, team_id, w, wanted, specialist)
    return {"spend_usd": {"value": _r(totals["now"], 4), "previous": _r(totals["before"], 4)},
            "comparison_spend_usd": {"value": _r(compared["now"], 4), "previous": _r(compared["before"], 4)},
            "cost_per_reviewed_pr_usd": {"value": _r(per_pr["now"], 4), "previous": _r(per_pr["before"], 4)},
            "reviewed_prs": prs["now"], "by_specialist": split(by_specialist), "by_model": split(by_model),
            "comparisons": scoring.comparisons(conn, team_id, specialist),
            "recommendations": scoring.recommendations(conn, team_id, specialist)}


FIX_LISTS = {"waiting": ("waiting",), "in_progress": ("queued", "drafting", "approved", "opened"),
             "done": ("merged", "closed"), "declined": ("declined", "expired", "failed")}
FIX_LIST_MAX = 50


def _fix_item(row: Any, spent: dict[str, float], names: dict[str, str], conn: Connection, team_id: str,
              now: datetime) -> dict[str, Any]:
    evidence = row.evidence or {}
    cost = row.cost_usd if row.cost_usd is not None else spent.get(row.run_id)
    after = fixes.flags(conn, team_id, evidence, row.merged_at, now) if row.state == "merged" and row.merged_at \
        else None
    return {"id": row.id, "title": row.title, "state": row.state, "kind": row.kind,
            "kind_words": fixes.KIND_WORDS.get(row.kind) if row.kind else None,
            "pattern": {"label": evidence.get("label"), "repo": evidence.get("repo"), "repo_id": row.repo_row_id,
                        "area": evidence.get("area"), "flags": evidence.get("flags"), "days": evidence.get("days")},
            "rationale": row.rationale, "diff": row.diff_preview, "has_patch": bool(row.has_patch), "risk": row.risk,
            "gate_id": row.gate_id, "pr": {"number": row.pr_number, "url": row.pr_url} if row.pr_number else None,
            "created_at": row.created_at, "updated_at": row.updated_at, "decided_at": row.decided_at,
            "decided_by": names.get(row.decided_by), "decline_reason": row.decline_reason,
            "failure_reason": row.failure_reason, "merged_at": row.merged_at, "flags_before": row.flags_before,
            "flags_after": after, "cost_usd": _r(float(cost), 4) if cost is not None else None}


def fixes_view(conn: Connection, team_id: str, now: datetime) -> dict[str, Any]:
    """The team's fix proposals by status: waiting oldest first, since the oldest expires first, the rest newest
    first, each list capped."""
    fp = fix_proposals.c
    # The patch itself stays behind its own route.
    shown = [c for c in fix_proposals.c if c.name != "patch"] + [fp.patch.is_not(None).label("has_patch")]
    found: dict[str, list[Any]] = {}
    for status, states in FIX_LISTS.items():
        order = (fp.created_at, fp.id) if status == "waiting" else (fp.updated_at.desc(), fp.id)
        found[status] = conn.execute(select(*shown).where(fp.team_id == team_id, fp.state.in_(states))
                                     .order_by(*order).limit(FIX_LIST_MAX)).all()
    rows = [r for listed in found.values() for r in listed]
    run_ids = {r.run_id for r in rows if r.run_id and r.cost_usd is None}
    spent = {r.run_id: float(r.cost or 0) for r in conn.execute(
        select(llm_calls.c.run_id, func.sum(llm_calls.c.cost_usd).label("cost")).where(
            llm_calls.c.team_id == team_id, llm_calls.c.cached.is_(False), llm_calls.c.run_id.in_(run_ids))
        .group_by(llm_calls.c.run_id))} if run_ids else {}
    deciders = {r.decided_by for r in rows if r.decided_by}
    names = {u.id: u.username for u in conn.execute(select(users.c.id, users.c.username)
                                                   .where(users.c.id.in_(deciders)))} if deciders else {}
    return {status: [_fix_item(r, spent, names, conn, team_id, now) for r in listed]
            for status, listed in found.items()}


# Needs attention


def _anonymous(brief: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in brief.items() if k not in ("author", "author_id", "author_login")}


def attention(team: Team, w: Window, spots: list[dict[str, Any]] | None = None, *,
              with_people: bool = True) -> list[dict[str, Any]]:
    """At most ATTENTION_MAX items in the brief's order, as structured data the client words. ``spots`` is every
    hotspot file in the window, when the caller has it. Without ``with_people`` no item names a person."""
    brief = team.pr_brief if with_people else (lambda pr: _anonymous(team.pr_brief(pr)))
    items: list[dict[str, Any]] = []
    bugs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in sorted(team.links, key=lambda link: _fixed_at(team, link), reverse=True):
        fixer, broke = team.pr(link.from_pr_row_id), team.pr(link.to_pr_row_id)
        if (fixer is None or broke is None or broke.row.merged_at_gh is None or broke.id in seen
                or _fixed_at(team, link) not in w):
            continue
        dropped = [c for c in broke.candidates if link in _became_bug(team, c)]
        if dropped:
            seen.add(broke.id)
            flag = dropped[0]
            bugs.append({"kind": "dropped_flag_bug", "link": link.kind, "confidence": link.confidence,
                         "pr": brief(broke), "bug_pr": brief(fixer),
                         "flag": {"category": flag.label, "claim": flag.row.claim,
                                  "specialist": flag.row.specialist}})
    items += bugs[:DROPPED_FLAG_ITEMS]
    for repo_id, repo in sorted(team.repos.items(), key=lambda kv: kv[1].full_name):
        now, before = review_rounds(team, w, repo_id), review_rounds(team, w.previous(), repo_id)
        if now is None or not before or now <= before * ROUNDS_RISE:
            continue
        extra = Counter(c.label for pr in team.merged(w, repo_id) for c in pr.candidates
                        if c.label and c.head and c.head != _first_head(pr))
        items.append({"kind": "rounds_rising", "repo_id": repo_id, "repo": repo.full_name, "value": _r(now),
                      "previous": _r(before), "top_category": min(extra, key=lambda k: (-extra[k], k)) if extra else None,
                      "categories": [{"category": k, "count": n} for k, n in
                                     sorted(extra.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]})
    every = spots if spots is not None else hotspots(team, w, limit=None)
    by_file = sorted((r for r in every if r["conflict_hours"]), key=lambda r: -r["conflict_hours"])
    if by_file and (len(by_file) == 1 or by_file[0]["conflict_hours"] > CONFLICT_LEAD * by_file[1]["conflict_hours"]):
        top = by_file[0]
        items.append({"kind": "conflict_file", "repo_id": top["repo_id"], "repo": top["repo"], "path": top["path"],
                      "hours": top["conflict_hours"], "prs": top["conflict_prs"],
                      "next_hours": by_file[1]["conflict_hours"] if len(by_file) > 1 else 0.0,
                      "files": [{"path": r["path"], "hours": r["conflict_hours"]} for r in by_file[:4]]})
    waiting: dict[str, list[PR]] = defaultdict(list)
    for pr in team.open_prs:
        if (not pr.row.draft and pr.first_review is None and w.end - pr.row.created_at_gh > WAITING_TOO_LONG
                and pr.row.author_person_id and team.human(pr.row.author_person_id, pr.row.author_login)):
            waiting[pr.row.author_person_id].append(pr)
    stuck = sorted(((person_id, sorted(prs, key=lambda pr: pr.row.created_at_gh))
                    for person_id, prs in waiting.items() if len(prs) >= WAITING_PRS),
                   key=lambda kv: (-len(kv[1]), team.name(kv[0]) or ""))

    def waited(pr: PR) -> dict[str, Any]:
        return {**brief(pr), "waiting_days": _r(_hours(w.end - pr.row.created_at_gh) / 24, 1)}

    if with_people:
        items += [{"kind": "waiting_on_review", "person_id": person_id, "person": team.name(person_id),
                   "login": team.logins.get(person_id),
                   "prs": [waited(pr) for pr in prs]} for person_id, prs in stuck]
    elif stuck:
        every_pr = sorted((pr for _, prs in stuck for pr in prs), key=lambda pr: pr.row.created_at_gh)
        items.append({"kind": "waiting_on_review", "count": len(every_pr), "prs": [waited(pr) for pr in every_pr]})
    bugs_by = Counter()
    for c in team.candidates:
        if any(_fixed_at(team, link) in w for link in _became_bug(team, c)):
            bugs_by[c.row.specialist] += 1
    items += [{"kind": "specialist_dropped_bugs", "specialist": name, "count": n}
              for name, n in sorted(bugs_by.items(), key=lambda kv: (-kv[1], kv[0])) if n >= 2]
    return items[:ATTENTION_MAX]


def _first_head(pr: PR) -> str | None:
    reviewed = sorted((r for r in pr.reviews if r.head_sha and r.submitted_at), key=lambda r: r.submitted_at)
    return reviewed[0].head_sha if reviewed else None


# Screens


def _since(w: Window, *more: timedelta) -> datetime:
    return min([w.previous().start, w.start - TRAILING, *(w.end - d for d in more)])


def scope(w: Window, days: int, repo_id: str | None) -> dict[str, Any]:
    return {"days": days, "repo_id": repo_id, "start": _iso(w.start), "end": _iso(w.end),
            "previous_start": _iso(w.previous().start)}


def repos_list(conn: Connection, team_id: str) -> list[dict[str, Any]]:
    return [repo_row(r) for r in conn.execute(select(gh_repos).where(gh_repos.c.team_id == team_id)
                                              .order_by(gh_repos.c.full_name, gh_repos.c.id))]


def repo_row(row: Any) -> dict[str, Any]:
    return {"id": row.id, "full_name": row.full_name, "name": row.full_name.rpartition("/")[2],
            "enabled": row.enabled, "project_id": row.project_id, "tracked_at": _iso(row.created_at)}


def overview(conn: Connection, team_id: str, now: datetime, days: int, repo_id: str | None, *,
             with_people: bool) -> dict[str, Any]:
    w = window(now, days)
    team = Team(conn, team_id, _since(w), now, repo_id=repo_id, children=FOR_OVERVIEW)
    shown = [repo_id] if repo_id else sorted(team.repos, key=lambda r: team.repos[r].full_name)
    spots = hotspots(team, w, limit=None)
    out: dict[str, Any] = {
        "scope": scope(w, days, repo_id), "dates": [_iso(d) for d in days_of(w)],
        "metrics": [{"id": key, "unit": unit, **_pair(lambda span, m=measure: m(team, span, repo_id), w, digits),
                     "series": daily(team, w, measure, shown, digits)}
                    for key, measure, unit, digits in OVERVIEW_METRICS],
        "attention": attention(team, w, spots, with_people=with_people),
    }
    if with_people:
        rows, _, _ = people_rows(team, w, spots)
        out["people"] = [{"id": r["id"], "name": r["name"], "login": r["login"], "repo": r["repo"],
                          "merged": r["shipping"]["merged"],
                          "rounds": r["quality"]["rounds_per_pr"], "reviews": r["review"]["reviews_given"],
                          "first_review_hours": r["review"]["first_review_hours"]} for r in rows]
    return out


def repo_view(conn: Connection, team_id: str, now: datetime, days: int, repo_id: str) -> dict[str, Any]:
    w = window(now, days)
    team = Team(conn, team_id, _since(w), now, repo_id=repo_id, children=FOR_REPO)
    return {
        "scope": scope(w, days, repo_id), "repo": repo_row(team.repos[repo_id]),
        "dates": [_iso(d) for d in days_of(w)],
        "time_to_merge": {"unit": "days", **_pair(lambda span: time_to_merge_days(team, span, repo_id), w)},
        "stages": stages(team, w, repo_id),
        "tiles": [{"id": key, "unit": unit, **_pair(lambda span, m=measure: m(team, span, repo_id), w, digits),
                   "series": daily(team, w, measure, [repo_id], digits)[repo_id]}
                  for key, measure, unit, digits in REPO_TILES],
        "hotspots": hotspots(team, w, repo_id),
        "push_kinds": push_kinds(team, w, repo_id),
    }


def people_view(conn: Connection, team_id: str, now: datetime, days: int, repo_id: str | None,
                view: str) -> dict[str, Any]:
    w = window(now, days)
    team = Team(conn, team_id, _since(w), now, repo_id=repo_id, children=FOR_PEOPLE)
    rows, median, _ = people_rows(team, w)
    previous, _, _ = people_rows(team, w.previous())
    before = {r["id"]: r[view] for r in previous}
    return {"scope": scope(w, days, repo_id), "view": view,
            "rows": [{"id": r["id"], "name": r["name"], "login": r["login"], "repo": r["repo"], "measures": r[view],
                      "previous": before.get(r["id"])} for r in rows],
            "team_median": median[view], "armature_reviews": armature_reviews(team, w)}


TREND = (("shipping", "merged", 0), ("quality", "rounds_per_pr", None), ("review", "reviews_given", 0),
         ("unblocking", "hotspot_prs", 0))


def person_view(conn: Connection, team_id: str, now: datetime, days: int, person_id: str) -> dict[str, Any]:
    w = window(now, days)
    team = Team(conn, team_id, _since(w, timedelta(weeks=TREND_WEEKS)), now, children=FOR_PEOPLE)
    person = team.people[person_id]
    rows, median, hot = people_rows(team, w)
    mine = next((r for r in rows if r["id"] == person_id), None)
    measures = mine or {"repo": _main_repo(team, person_id), **person_measures(team, w, person_id, hot)}
    previous = person_measures(team, w.previous(), person_id, _hot(hotspots(team, w.previous())))
    ends = [now - timedelta(weeks=TREND_WEEKS - 1 - i) for i in range(TREND_WEEKS)]
    trend: dict[str, Any] = {"weeks": [_iso(e) for e in ends], "person": defaultdict(list),
                             "team_median": defaultdict(list)}
    for end in ends:
        week_rows, week_median, _ = people_rows(team, Window(end - timedelta(weeks=1), end))
        # Someone with no row that week merged and reviewed nothing, so their measures are the empty ones.
        week_mine = next((r for r in week_rows if r["id"] == person_id), None)
        for area, key, empty in TREND:
            trend["person"][area].append(week_mine[area][key] if week_mine else empty)
            trend["team_median"][area].append(week_median.get(area, {}).get(key))
    shipped = sorted((pr for pr in team.by_author.get(person_id, ()) if pr.row.merged_at_gh in w),
                     key=lambda pr: pr.row.merged_at_gh, reverse=True)
    flagged = Counter(c.label for pr in team.by_author.get(person_id, ())
                      for c in pr.candidates if c.label and c.row.created_at in w)
    comments = defaultdict(list)
    for pr, c in team.comments_by.get(person_id, ()):
        comments[(pr.id, c.gh_review_id)].append(c)
    reviews = sorted(((pr, r) for pr, r in team.reviews_by.get(person_id, ())
                      if r.submitted_at in w and pr.row.author_person_id != person_id),
                     key=lambda pair: pair[1].submitted_at, reverse=True)
    open_prs = sorted((pr for pr in team.by_author.get(person_id, ()) if pr.row.state == "open"),
                      key=lambda pr: pr.row.created_at_gh)
    return {
        "scope": scope(w, days, None),
        "person": {"id": person.id, "name": person.display_name, "login": team.logins.get(person.id),
                   "repo": measures["repo"],
                   "repos": sorted({pr.repo.full_name for pr in team.by_author.get(person_id, ())})},
        **{area: measures[area] for area in VIEWS},
        "previous": {area: previous[area] for area in VIEWS},
        "team_median": median,
        "trend": {"weeks": trend["weeks"], "person": dict(trend["person"]),
                  "team_median": dict(trend["team_median"])},
        "merged_prs": [{**team.pr_brief(pr), "merged_at": _iso(pr.row.merged_at_gh), "change_type": pr.change_type,
                        "size": (size_of(pr.row.additions, pr.row.deletions) or (None,))[0], "rounds": pr.rounds}
                       for pr in shipped],
        "flagged_categories": [{"category": k, "count": n} for k, n in
                               sorted(flagged.items(), key=lambda kv: (-kv[1], kv[0]))],
        "reviews": [{"pr": team.pr_brief(pr), "state": r.state, "at": _iso(r.submitted_at), "url": r.url,
                     "comments": [{"url": c.url, "path": c.path, "line": c.line,
                                   "substantive": (team.comment_labels.get(c.id) == SUBSTANTIVE
                                                   if c.id in team.comment_labels else None)}
                                  for c in comments.get((pr.id, r.gh_review_id), ())]} for pr, r in reviews],
        "hotspot_prs": [team.pr_brief(pr) for pr in shipped if any((pr.repo.id, p) in hot for p in pr.paths())],
        "open_prs": [{**team.pr_brief(pr), "age_days": _r(_hours(now - pr.row.created_at_gh) / 24, 1),
                      "rounds": pr.rounds, "waiting_on": _waiting_on(pr)} for pr in open_prs],
    }


def _waiting_on(pr: PR) -> str:
    """review before any review, author after changes were asked for, merge once approved, else review."""
    if not pr.human_reviews:
        return "review"
    last = {r.reviewer_person_id: r.state for r in pr.human_reviews if r.state in ("approved", "changes_requested")}
    if "changes_requested" in last.values():
        return "author"
    return "merge" if "approved" in last.values() else "review"


def findings_screen(conn: Connection, team_id: str, now: datetime, days: int, *, repo_id: str | None,
                    category: str | None, person_id: str | None) -> dict[str, Any]:
    w = window(now, days)
    team = Team(conn, team_id, _since(w, timedelta(weeks=WEEKS)), now, children=FOR_FINDINGS)
    return {"scope": scope(w, days, repo_id), "category": category, "person_id": person_id,
            **findings_view(team, w, repo_id=repo_id, category=category, person_id=person_id)}


def pr_detail(conn: Connection, team_id: str, pr_id: str, now: datetime) -> dict[str, Any] | None:
    """A pull request's timeline: pushes by kind, each review round with its flags, conflicts, checks, merge, and
    links. None when the team has no such pull request."""
    exists = conn.execute(select(gh_pull_requests.c.created_at_gh).where(
        gh_pull_requests.c.team_id == team_id, gh_pull_requests.c.id == pr_id)).first()
    if exists is None:
        return None
    team = Team(conn, team_id, exists.created_at_gh, now, pr_id=pr_id)
    pr = team.prs[pr_id]
    name = pr.repo.full_name
    by_head: dict[str | None, dict[str, Any]] = {}

    def round_for(head: str | None) -> dict[str, Any]:
        return by_head.setdefault(head, {"head_sha": head, "at": None, "reviews": [], "comments": [], "flags": []})

    for r in sorted((r for r in pr.reviews if r.submitted_at is not None), key=lambda r: r.submitted_at):
        entry = round_for(r.head_sha if r.head_sha in pr.heads else None)
        entry["at"] = entry["at"] or _iso(r.submitted_at)
        entry["reviews"].append({"reviewer": team.name(r.reviewer_person_id) or r.reviewer_login,
                                 "reviewer_login": team.logins.get(r.reviewer_person_id),
                                 "reviewer_id": r.reviewer_person_id, "state": r.state, "at": _iso(r.submitted_at),
                                 "url": r.url, "armature": r.actor_source == ARMATURE})
    for head in sorted(pr.posted_heads - {r.head_sha for r in pr.reviews}):
        round_for(head)
    for c in sorted(pr.comments, key=lambda c: c.created_at_gh):
        round_for(c.head_sha if c.head_sha in by_head else None)["comments"].append(
            {"author": team.name(c.author_person_id) or c.author_login,
             "author_login": team.logins.get(c.author_person_id), "path": c.path, "line": c.line,
             "at": _iso(c.created_at_gh), "url": c.url,
             "line_url": line_url(pr.web, name, c.head_sha, c.path, c.line) if c.path else None})
    for c in sorted(pr.candidates, key=lambda c: (c.row.created_at, c.row.specialist, c.row.ordinal)):
        round_for(c.head if c.head in by_head else None)["flags"].append(_flag(c))
    rounds = sorted(by_head.values(), key=lambda e: (e["head_sha"] is None, e["at"] or "~"))
    links = []
    for link in team.links:
        fixes = link.from_pr_row_id == pr_id
        other = team.pr(link.to_pr_row_id if fixes else link.from_pr_row_id)
        links.append({"kind": link.kind, "direction": "fixes" if fixes else "fixed_by", "confidence": link.confidence,
                      "pr": team.pr_brief(other) if other else None})
    return {
        "pr": {**team.pr_brief(pr), "created_at": _iso(pr.row.created_at_gh), "merged_at": _iso(pr.row.merged_at_gh),
               "closed_at": _iso(pr.row.closed_at_gh), "additions": pr.row.additions, "deletions": pr.row.deletions,
               "size": (size_of(pr.row.additions, pr.row.deletions) or (None,))[0], "change_type": pr.change_type,
               "head_sha": pr.row.head_sha, "base_ref": pr.row.base_ref, "draft": pr.row.draft,
               "rounds": pr.rounds},
        "pushes": [{"after_sha": p.after_sha, "before_sha": p.before_sha, "at": _iso(p.pushed_at),
                    "kind": p.push_class, "confidence": p.class_confidence, "forced": p.forced,
                    "armature": p.actor_source == ARMATURE,
                    "url": f"{pr.web}/{name}/commit/{p.after_sha}"}
                   for p in sorted(pr.pushes, key=lambda p: p.pushed_at)],
        "rounds": rounds,
        "conflicts": [{"start": _iso(a), "end": _iso(b), "hours": _r(_hours(b - a))} for a, b in pr.intervals],
        "checks": [{"name": c.name, "head_sha": c.head_sha, "status": c.status, "conclusion": c.conclusion,
                    "started_at": _iso(c.started_at), "completed_at": _iso(c.completed_at)}
                   for c in sorted(pr.checks, key=lambda c: (c.completed_at or c.created_at, c.name))],
        "merge": {"at": _iso(pr.row.merged_at_gh), "method": pr.row.merge_method,
                  "commit": pr.row.merge_commit_sha} if pr.row.merged_at_gh else None,
        "links": links,
    }


# Drill-down


STAGES = {"waiting_first_review": "waiting_first_review", "in_review": "in_review",
          "blocked": "blocked_by_conflicts", "waiting_on_ci": "waiting_on_ci",
          "approved_to_merged": "approved_to_merged"}
UNCLASSIFIED = "unclassified"
PR_LIST_MAX = 100
RECENT_PRS = 20
DROPPED_SHOWN = 20
FOR_FILE = frozenset({"reviews", "comments", "samples", "candidates", "files"})


def pr_id_by_number(conn: Connection, team_id: str, repo_id: str, number: int) -> str | None:
    return conn.execute(select(gh_pull_requests.c.id).where(
        gh_pull_requests.c.team_id == team_id, gh_pull_requests.c.repo_row_id == repo_id,
        gh_pull_requests.c.number == number)).scalar()


def person_by_login(conn: Connection, team_id: str, login: str) -> str | None:
    """The person a GitHub login names now: the identity seen most recently, since logins move between people."""
    return conn.execute(select(person_identities.c.person_id).where(
        person_identities.c.team_id == team_id, person_identities.c.kind == "github_login",
        person_identities.c.value == login.lower())
        .order_by(person_identities.c.last_seen.desc(), person_identities.c.id).limit(1)).scalar()


def _branch(conn: Connection, team: Team, repo: Any) -> str | None:
    """The repo's default branch: its project's, else the base of its newest pull request."""
    if repo.project_id:
        found = conn.execute(select(projects.c.default_branch).where(projects.c.id == repo.project_id)).scalar()
        if found:
            return found
    newest = max((pr for pr in team.prs.values() if pr.repo.id == repo.id and pr.row.base_ref),
                 key=lambda pr: pr.row.created_at_gh, default=None)
    return newest.row.base_ref if newest else None


def file_view(conn: Connection, team_id: str, now: datetime, days: int, repo_id: str, path: str,
              *, with_people: bool) -> dict[str, Any]:
    """One file of a repo: its link on GitHub, its flags and conflict hours each week for 12 weeks, and the pull
    requests that touched it most recently."""
    w = window(now, days)
    team = Team(conn, team_id, _since(w, timedelta(weeks=WEEKS)), now, repo_id=repo_id, children=FOR_FILE)
    repo = team.repos[repo_id]
    touched = [pr for pr in team.prs.values() if path in pr.paths()]
    ends = [now - timedelta(weeks=WEEKS - 1 - i) for i in range(WEEKS)]

    def flags(span: Window) -> int:
        return (sum(1 for c in team.flags(span) if c.repo_id == repo_id and c.row.path == path)
                + sum(1 for pr, c in team.root_comments.within(span) if pr.repo.id == repo_id and c.path == path))

    def conflicts(span: Window) -> float:
        return sum(pr.conflict_hours(span) for pr in team.conflicted if pr.files and path in pr.files)

    weeks = [Window(end - timedelta(weeks=1), end) for end in ends]
    brief = team.pr_brief if with_people else (lambda pr: _anonymous(team.pr_brief(pr)))
    recent = sorted(touched, key=lambda pr: pr.row.merged_at_gh or pr.row.created_at_gh, reverse=True)
    return {"scope": scope(w, days, repo_id), "repo": repo_row(repo), "path": path,
            "url": blob_url(team.web[repo_id], repo.full_name, _branch(conn, team, repo), path),
            "findings": {"value": flags(w), "previous": flags(w.previous())},
            "conflict_hours": {"value": _r(conflicts(w)), "previous": _r(conflicts(w.previous()))},
            "churn": {"value": sum(1 for pr in team.merged(w, repo_id) if path in pr.paths()),
                      "previous": sum(1 for pr in team.merged(w.previous(), repo_id) if path in pr.paths())},
            "weekly": {"weeks": [_iso(end) for end in ends], "findings": [flags(span) for span in weeks],
                       "conflict_hours": [_r(conflicts(span)) for span in weeks]},
            "prs": [{**brief(pr), "merged_at": _iso(pr.row.merged_at_gh), "created_at": _iso(pr.row.created_at_gh)}
                    for pr in recent[:RECENT_PRS]]}


def pr_list(conn: Connection, team_id: str, now: datetime, days: int, *, repo_id: str | None, stage: str | None,
            push_class: str | None, category: str | None, person_id: str | None,
            with_people: bool) -> dict[str, Any]:
    """Pull requests for a drill-down. With a stage, the PRs merged in the window, longest in that stage first.
    Without one, those merged or opened in the window, newest first. Every filter given narrows the list."""
    w = window(now, days)
    team = Team(conn, team_id, _since(w), now, repo_id=repo_id, children=FOR_REPO)
    if stage is not None:
        chosen = team.merged(w, repo_id)
    else:
        chosen = list({pr.id: pr for pr in (*team.merged(w, repo_id), *team.opened(w, repo_id))}.values())
    if push_class is not None:
        wanted = None if push_class == UNCLASSIFIED else push_class
        chosen = [pr for pr in chosen if any(p.push_class == wanted and p.pushed_at in w for p in pr.pushes)]
    if category is not None:
        chosen = [pr for pr in chosen if any(c.label == category for c in pr.candidates)]
    if person_id is not None:
        chosen = [pr for pr in chosen if pr.row.author_person_id == person_id]
    brief = team.pr_brief if with_people else (lambda pr: _anonymous(team.pr_brief(pr)))
    rows = []
    for pr in chosen:
        hours = pr_stages(pr)[STAGES[stage]] if stage is not None else None
        rows.append({**brief(pr), "stage_hours": hours, "rounds": pr.rounds,
                     "merged_at": _iso(pr.row.merged_at_gh), "created_at": _iso(pr.row.created_at_gh),
                     "_at": pr.row.merged_at_gh or pr.row.created_at_gh})
    if stage is not None:
        rows.sort(key=lambda r: (-(r["stage_hours"] or 0), r["repo"], r["number"]))
    else:
        rows.sort(key=lambda r: (r["_at"], r["repo"], r["number"]), reverse=True)
    for row in rows:
        del row["_at"]
    return {"scope": scope(w, days, repo_id), "stage": stage, "push_class": push_class, "category": category,
            "person_id": person_id, "total": len(rows), "prs": rows[:PR_LIST_MAX]}


def specialist_view(conn: Connection, team_id: str, now: datetime, days: int, specialist: str,
                    *, with_people: bool) -> dict[str, Any]:
    """A specialist's most recent dropped flags in the window, newest first."""
    w = window(now, days)
    team = Team(conn, team_id, w.start, now, children=FOR_FINDINGS)
    brief = team.pr_brief if with_people else (lambda pr: _anonymous(team.pr_brief(pr)))
    raised = [c for c in team.flags(w) if c.row.specialist == specialist]
    dropped = sorted((c for c in raised if c.status == "dropped"),
                     key=lambda c: (c.row.created_at, c.row.id), reverse=True)
    return {"scope": scope(w, days, None), "specialist": specialist, "raised": len(raised),
            "dropped": len(dropped),
            "flags": [{**_flag(c), "pr": brief(c.pr) if c.pr else None} for c in dropped[:DROPPED_SHOWN]]}
