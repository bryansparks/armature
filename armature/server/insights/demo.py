"""Invented Insights history for a demo team, so every Insights screen shows believable numbers on a dev server.

A demo team is one that carries the DEMO_SETTING setting, which ``seed`` writes when it makes the team. ``seed`` refuses
while the database holds any other team unless forced, never writes into a team without the setting, and replaces the
demo team's earlier invented history instead of adding to it. The same clock gives the same history.
"""
from __future__ import annotations

import hashlib
import itertools
import random
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, Table, delete, insert, or_, select, update

from armature.server import audit, conversations
from armature.server.db import new_id
from armature.server.db.schema import (
    candidate_matches,
    cluster_members,
    clusters,
    finding_candidates,
    findings,
    fix_proposals,
    gates,
    gh_check_runs,
    gh_conflict_samples,
    gh_pr_commits,
    gh_pr_files,
    gh_pr_links,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_review_comments,
    gh_reviews,
    gh_sync_state,
    jobs,
    labels,
    llm_calls,
    people,
    person_identities,
    runs,
    settings,
    teams,
    tool_calls,
)
from armature.server.insights import attribution, fixes, metrics, pushes, taxonomy
from armature.server.insights.candidates import MATCHER_VERSION

DEMO_SETTING = "insights.demo"
DEMO_ACTOR = "insights-demo"
SEED = 2026
HISTORY_DAYS = 190
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ARMATURE_LOGIN = "armature-bot[bot]"
CHUNK = 500

IOS, ANDROID, SERVICE = "acme/mobile-ios", "acme/android-tree", "acme/tree-service"
GRADLE = "app/build.gradle.kts"
SYNC_WORKER = "app/src/main/java/com/acme/tree/sync/SyncWorker.kt"
TREE_CACHE = "app/src/main/java/com/acme/tree/cache/TreeCache.kt"
TREE_DETAIL = "app/src/main/java/com/acme/tree/detail/TreeDetailFragment.kt"
MAP_MODEL = "TreeApp/Features/Map/MapViewModel.swift"
PIN_CLUSTERER = "TreeApp/Features/Map/PinClusterer.swift"
HTTP_LOGGING = "internal/httpx/logging.go"


class Refused(Exception):
    """Seeding would touch a team that isn't a demo team."""


@dataclass(frozen=True)
class RepoSpec:
    full_name: str
    files: tuple[str, ...]
    conflict_file: str
    conflict_share: float
    conflict_hours: tuple[float, float]
    checks: tuple[str, ...]
    specialists: tuple[str, ...]
    areas: tuple[str, ...]
    upkeep: tuple[str, ...]
    first_number: int


REPOS = (
    RepoSpec(IOS, (
        "TreeApp/Features/TreeDetail/TreeDetailView.swift", "TreeApp/Features/TreeDetail/TreeDetailViewModel.swift",
        MAP_MODEL, PIN_CLUSTERER, "TreeApp/Features/Upload/PhotoUploader.swift",
        "TreeApp/Features/Onboarding/OnboardingFlow.swift", "TreeApp/Sync/OfflineStore.swift", "TreeApp/Sync/SyncEngine.swift",
        "TreeApp/Search/SearchView.swift", "TreeApp/Settings/SettingsView.swift", "TreeApp/Networking/APIClient.swift",
        "TreeApp/Resources/Localizable.xcstrings", "TreeAppTests/SyncEngineTests.swift"),
        "TreeApp.xcodeproj/project.pbxproj", 0.07, (1.0, 5.0), ("build", "unit-tests", "ui-tests", "swiftlint"),
        ("concurrency", "correctness", "security", "performance", "testing", "conventions", "ui"),
        ("tree detail", "map clustering", "photo upload", "onboarding", "offline sync", "search", "settings",
         "push notifications", "share sheet", "home screen widget"),
        ("Bump Xcode to 16.2 on CI", "Update SwiftLint rules", "Move fastlane lanes to the new match setup",
         "Drop iOS 16 support"), 1180),
    RepoSpec(ANDROID, (
        SYNC_WORKER, "app/src/main/java/com/acme/tree/list/TreeListViewModel.kt",
        "app/src/main/java/com/acme/tree/list/TreeListScreen.kt", TREE_DETAIL,
        "app/src/main/java/com/acme/tree/camera/CaptureController.kt",
        "app/src/main/java/com/acme/tree/species/SpeciesPicker.kt", TREE_CACHE,
        "app/src/main/java/com/acme/tree/map/MapScreen.kt", "app/src/main/java/com/acme/tree/auth/LoginViewModel.kt",
        "app/src/main/res/values/strings.xml", "app/src/test/java/com/acme/tree/sync/SyncWorkerTest.kt",
        "gradle/libs.versions.toml"),
        GRADLE, 0.24, (3.0, 18.0), ("assembleDebug", "unit-tests", "detekt", "instrumented-tests"),
        ("concurrency", "correctness", "security", "performance", "testing", "conventions", "ui"),
        ("tree list", "sync worker", "camera capture", "species picker", "offline cache", "map screen", "login",
         "notifications", "photo gallery", "deep links"),
        ("Bump AGP to 8.7", "Update Kotlin to 2.1", "Upgrade the Compose BOM", "Enable R8 full mode"), 3420),
    RepoSpec(SERVICE, (
        "internal/search/handler.go", "internal/search/query.go", "internal/species/index.go",
        "internal/photos/pipeline.go", "internal/auth/tokens.go", "internal/ratelimit/limiter.go",
        "internal/export/job.go", "internal/geo/bbox.go", "internal/audit/log.go", HTTP_LOGGING,
        "migrations/0042_tree_tags.sql", "internal/search/handler_test.go"),
        "go.mod", 0.05, (1.0, 4.0), ("build", "test", "golangci-lint"),
        ("concurrency", "correctness", "security", "performance", "testing", "conventions", "data"),
        ("tree search API", "species index", "photo pipeline", "auth tokens", "rate limiter", "export job",
         "geo queries", "audit log", "billing webhooks", "cache layer"),
        ("Bump Go to 1.23", "Upgrade the Postgres driver", "Update golangci-lint config", "Pin base image digests"),
        815),
)

# Name, home repository, and how slow a reviewer they are (1 is the team's usual pace).
PEOPLE = (
    ("Priya Raman", IOS, 0.7), ("Kenji Watanabe", IOS, 1.4), ("Sofia Lindqvist", IOS, 1.0), ("Mei Chen", IOS, 2.1),
    ("Mateo Alvarez", ANDROID, 1.2), ("Aisha Bello", ANDROID, 0.6), ("Daniel Okafor", ANDROID, 1.8),
    ("Hannah Kowalski", ANDROID, 1.0), ("Tomás Herrera", SERVICE, 0.9), ("Omar Haddad", SERVICE, 1.5),
    ("Grace Mensah", SERVICE, 0.8), ("Lucas Ferreira", SERVICE, 2.3),
)
BOT = ("renovate[bot]", (IOS, ANDROID))

CHANGE_WEIGHTS = (("bug-fix", 30), ("enhancement", 28), ("new-feature", 16), ("code-maintenance", 16),
                  (metrics.PLATFORM, 10))
TITLES = {
    "bug-fix": ("Fix {area} crash on an empty list", "Fix {area} not refreshing after an edit",
                "Fix the wrong count in {area}", "Fix {area} timing out on slow networks"),
    "enhancement": ("Speed up {area} loading", "Show progress in {area}", "Retry failed {area} requests",
                    "Polish the {area} empty state"),
    "new-feature": ("Add filters to {area}", "Add offline mode to {area}", "Add sharing to {area}",
                    "Introduce {area} v2"),
    "code-maintenance": ("Move {area} into its own module", "Remove dead code in {area}",
                         "Rename the {area} models", "Add tests for {area}"),
}
# Lines-changed bounds and their weights, ordered XS to XL, for most changes and for platform upkeep.
SIZES = ((2, 9), (10, 29), (30, 99), (100, 499), (500, 1400))
SIZE_WEIGHTS = (12, 24, 32, 24, 8)
UPKEEP_WEIGHTS = (30, 40, 20, 8, 2)

SPECIALISTS = {
    "concurrency": ("concurrency",),
    "correctness": ("correctness", "crash-safety", "error-handling"),
    "security": ("security-privacy",),
    "performance": ("performance",),
    "testing": ("testing",),
    "conventions": ("conventions", "simplicity"),
    "ui": ("ui-layout", "accessibility", "localization"),
    "data": ("data-contract", "api-misuse"),
}
CLAIMS = {
    "concurrency": ("{name} mutates shared state from a background thread without a lock",
                    "{name} updates UI state off the main thread"),
    "correctness": ("{name} compares the wrong field, so edited trees keep their old species",
                    "Off-by-one in {name} drops the last page of results"),
    "crash-safety": ("Force unwrap in {name} crashes when the response has no photo",
                     "{name} indexes the list without checking it is non-empty"),
    "error-handling": ("{name} swallows the network error and shows an empty list",
                       "{name} retries forever on a 4xx response"),
    "security-privacy": ("{name} logs the user's location at info level",
                         "{name} builds the query by string concatenation"),
    "performance": ("{name} decodes full-size images on the main thread", "{name} runs a query per row in a loop"),
    "testing": ("No test covers the empty state added in {name}",
                "The new test in {name} can't fail, because it asserts on the mock"),
    "conventions": ("{name} names its types differently from the rest of the module",
                    "{name} puts view logic in the repository layer"),
    "simplicity": ("{name} reimplements the date formatter the app already has",
                   "Dead branch in {name} after the flag removal"),
    "ui-layout": ("{name} has no loading state, so the screen flashes empty",
                  "{name} hard-codes padding instead of the spacing tokens"),
    "accessibility": ("The icon button in {name} has no accessibility label", "{name} ignores dynamic type"),
    "localization": ("{name} hard-codes an English string", "{name} builds a plural by appending an s"),
    "data-contract": ("{name} renames a JSON field the apps still read", "{name} drops a column still in use"),
    "api-misuse": ("{name} calls a deprecated API", "{name} reads a response body after closing it"),
}
# Model, provider, and USD per million input and output tokens, by specialist.
MODELS = {
    "lead": ("claude-opus-4-6", "anthropic", 5.0, 25.0),
    "concurrency": ("claude-opus-4-6", "anthropic", 5.0, 25.0),
    "correctness": ("claude-sonnet-4-6", "anthropic", 3.0, 15.0),
    "security": ("claude-sonnet-4-6", "anthropic", 3.0, 15.0),
    "performance": ("gemini-2.5-pro", "gemini", 1.25, 10.0),
    "testing": ("claude-haiku-4-5", "anthropic", 1.0, 5.0),
    "conventions": ("claude-haiku-4-5", "anthropic", 1.0, 5.0),
    "ui": ("claude-sonnet-4-6", "anthropic", 3.0, 15.0),
    "data": ("claude-sonnet-4-6", "anthropic", 3.0, 15.0),
}
# The demo's proposed fixes: category, repository, area, state, kind of fix, days since proposed, and extras.
FIXES = (
    ("concurrency", ANDROID, "app/src", "waiting", "check", 2, {"flags": 23}),
    ("performance", ANDROID, "app/src", "waiting", "sweep", 1, {"flags": 11}),
    ("crash-safety", IOS, "TreeApp/Features", "drafting", None, 0, {"flags": 8}),
    ("error-handling", SERVICE, "internal/search", "queued", None, 0, {"flags": 6}),
    ("security-privacy", SERVICE, "internal/httpx", "opened", "check", 9, {"flags": 7, "pr": 958}),
    ("performance", SERVICE, "internal/photos", "approved", "sweep", 3, {"flags": 6}),
    ("simplicity", ANDROID, "app/src", "merged", "check", 27, {"flags": 10, "pr": 3512, "merged_days_ago": 20}),
    ("correctness", SERVICE, "internal/species", "closed", "sweep", 30, {"flags": 6, "pr": 931}),
    ("localization", IOS, "TreeApp/Resources", "declined", "guidance", 12,
     {"flags": 7, "reason": "Strings move to the new catalog next sprint, so this would be redone."}),
    ("testing", SERVICE, "internal/search", "expired", "author_note", 16, {"flags": 5}),
    ("ui-layout", ANDROID, "app/src", "failed", None, 5, {"flags": 9}),
)
# The run a demo fix in this state has. A fix still moving has a run that hasn't ended, so it isn't failed on read.
RUN_OF_FIX = {"drafting": "running", "waiting": "waiting", "approved": "running", "failed": "failed"}
DIFFS = {
    "check": {"lines": [
        ["file", "config/detekt/detekt.yml"], ["add", "  MainThreadStateMutation:"], ["add", "    active: true"],
        ["file", "buildSrc/src/main/kotlin/rules/MainThreadStateMutation.kt"],
        ["add", "class MainThreadStateMutation(config: Config) : Rule(config) {"],
        ["add", "    override val issue = Issue(javaClass.simpleName, Severity.Defect,"],
        ["add", "        \"Shared state is written off the main thread without a lock.\", Debt.TWENTY_MINS)"],
        ["add", ""], ["add", "    override fun visitCallExpression(expression: KtCallExpression) {"],
        ["add", "        if (expression.isStateWrite() && !expression.isOnMainThread()) {"],
        ["add", "            report(CodeSmell(issue, Entity.from(expression), issue.description))"],
        ["file", "app/build.gradle.kts"], ["del", "    detektPlugins(libs.detekt.formatting)"],
        ["add", "    detektPlugins(project(\":buildSrc\"))"]]},
    "sweep": {"lines": [
        ["file", "app/src/main/java/com/acme/tree/detail/TreeDetailFragment.kt"],
        ["del", "        val bitmap = BitmapFactory.decodeFile(photo.path)"],
        ["add", "        val bitmap = withContext(Dispatchers.IO) { decodeScaled(photo.path, binding.photo.width) }"],
        ["file", "app/src/main/java/com/acme/tree/list/TreeListScreen.kt"],
        ["del", "    val thumb = remember(tree.photo) { BitmapFactory.decodeFile(tree.photo) }"],
        ["add", "    val thumb by produceState<Bitmap?>(null, tree.photo) {"],
        ["add", "        value = withContext(Dispatchers.IO) { decodeScaled(tree.photo, THUMB_PX) }"],
        ["add", "    }"], ["file", "app/src/main/java/com/acme/tree/camera/Images.kt"],
        ["add", "fun decodeScaled(path: String, targetPx: Int): Bitmap {"],
        ["add", "    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }"],
        ["add", "    BitmapFactory.decodeFile(path, bounds)"]]},
    "guidance": {"lines": [
        ["file", "CLAUDE.md"], ["add", "## Strings"], ["add", ""],
        ["add", "- Every user-facing string lives in Localizable.xcstrings, never in code."],
        ["add", "- Plurals use the catalog's plural variants, never an appended s."],
        ["add", "- Review flags any string literal passed to Text or a label."], ["add", ""]]},
    "author_note": {"lines": [
        ["file", "CONTRIBUTING.md"], ["add", "### Tests for empty states"], ["add", ""],
        ["add", "Each handler change adds a test for its empty result, asserting on the response body."],
        ["add", "A test that asserts only on a mock can't fail, so review flags it."], ["add", ""]]},
}

# How the lead decided a flag: kept, dropped by the lead, raised and never taken, or fixed in the same run.
STATUS_WEIGHTS = (("kept", 48), ("drop", 22), ("none", 22), ("fixed", 8))


@dataclass
class Repo:
    id: str
    spec: RepoSpec


@dataclass
class Dev:
    id: str
    name: str
    login: str
    gh: int
    repo: str
    speed: float
    human: bool = True


@dataclass
class Flag:
    specialist: str
    category: str | None
    path: str
    claim: str
    status: str
    round: int = 0
    severity: str = "medium"


@dataclass
class Plan:
    repo: str
    author: Dev
    created: datetime
    rounds: int
    change_type: str
    lines: int
    files: list[str]
    title: str
    conflict: float = 0.0
    reviewed: bool = True
    draft: bool = False
    closed: bool = False
    flags: list[Flag] | None = None
    number: int = 0
    id: str = field(default_factory=new_id)


def _login(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def _patch_text(diff: dict[str, Any]) -> str:
    """The demo diff's lines as the git diff a fix run stores."""
    out = []
    for kind, text in diff["lines"]:
        if kind == "file":
            out += [f"diff --git a/{text} b/{text}", f"--- a/{text}", f"+++ b/{text}", "@@ -1,1 +1,1 @@"]
        else:
            out.append(("+" if kind == "add" else "-") + text)
    return "\n".join(out) + "\n"


def _uuid(*parts: Any) -> str:
    return str(uuid.UUID(hex=_sha(*parts)[:32]))


def _sha(*parts: Any) -> str:
    return hashlib.sha1("/".join(map(str, parts)).encode()).hexdigest()


def _h(hours: float) -> timedelta:
    return timedelta(hours=hours)


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


class History:
    """One demo team's rows, built in memory from a seeded generator and the clock."""

    def __init__(self, team_id: str, now: datetime, armature_id: str, self_heal_id: str | None = None):
        self.team_id, self.now, self.armature_id, self.self_heal_id = team_id, now, armature_id, self_heal_id
        self.rng = random.Random(SEED)
        self.rows: dict[Table, list[dict[str, Any]]] = defaultdict(list)
        self.gh_ids = itertools.count(880_000_001)
        self.repos: dict[str, Repo] = {}
        self.devs: list[Dev] = []
        self.by_name: dict[str, Dev] = {}
        self.links: list[tuple[Plan, Plan, str, float, dict[str, Any]]] = []
        # Inserted one at a time after the bulk tables, since their rows don't all name the same columns.
        self.fix_rows: list[tuple[Table, dict[str, Any]]] = []

    def _one(self, table: Table, **values: Any) -> str:
        row_id = values.pop("id", None) or new_id()
        self.fix_rows.append((table, {"id": row_id, "team_id": self.team_id, **values}))
        return row_id

    def add(self, table: Table, **values: Any) -> str:
        row_id = values.pop("id", None) or new_id()
        if "team_id" in table.c:
            values["team_id"] = self.team_id
        self.rows[table].append({"id": row_id, **values})
        return row_id

    def build(self) -> dict[Table, list[dict[str, Any]]]:
        self._people()
        plans = self._plans() + self._stories()
        for spec in REPOS:
            number = spec.first_number
            for plan in sorted((p for p in plans if p.repo == spec.full_name), key=lambda p: p.created):
                number += self.rng.choice((1, 1, 1, 2, 3))
                plan.number = number
        for plan in sorted(plans, key=lambda p: p.created):
            self._pull_request(plan)
        for fixer, broke, kind, confidence, evidence in self.links:
            self.add(gh_pr_links, from_pr_row_id=fixer.id, to_pr_row_id=broke.id, kind=kind, evidence=evidence,
                     confidence=confidence, detector_version="1", created_at=fixer.created + _h(1))
        if self.self_heal_id:
            self._fixes()
        return self.rows

    def _fixes(self) -> None:
        """One proposed fix in every state. The waiting ones hold an open gate a team lead can decide."""
        for spec in FIXES:
            label, repo, area, state, kind, days_ago, extra = spec
            created = self.now - timedelta(days=days_ago)
            proposal_id = _uuid("fix", label, repo)
            branch = fixes.branch_of(proposal_id)
            paths = [p for p in self.repos[repo].spec.files if p.startswith(area)]
            examples = [{"claim": CLAIMS[label][n % 2].format(name=_name(path)), "path": path, "line": 40 + 17 * n}
                        for n, path in enumerate(paths[:6])]
            values: dict[str, Any] = {
                "repo_row_id": self.repos[repo].id, "project_id": None,
                "cluster_key": fixes.cluster_key(taxonomy.VERSION, label, repo, area), "kind": kind,
                "title": fixes.title_of(label, repo, area), "state": state, "branch": branch, "base": "main",
                "evidence": {"taxonomy_version": taxonomy.VERSION, "label": label, "repo": repo, "area": area,
                             "flags": extra.get("flags", 9), "days": 28, "examples": examples},
                "created_at": created, "updated_at": created + _h(2)}
            if state not in ("queued",):
                values["run_id"] = self._one(runs, project_id=None, workflow_name=fixes.SELF_HEAL,
                                            workflow_id=self.self_heal_id,
                                            status=RUN_OF_FIX.get(state, "succeeded"),
                                            trigger_kind="server", actor=DEMO_ACTOR, started_at=created,
                                            finished_at=None if state in RUN_OF_FIX and state != "failed"
                                            else created + _h(1),
                                            cost_usd=extra.get("cost", 2.4), head_sha=_sha("fix", proposal_id))
                values["cost_usd"] = extra.get("cost", 2.4)
            if kind and state not in ("queued", "drafting", "failed"):
                text = _patch_text(DIFFS[kind])
                values.update(patch=text, base_sha=_sha("base", repo), risk="low",
                              diff_preview=fixes.preview_of(fixes.patch_files(text)))
            if state == "waiting":
                job_id = self._one(jobs, project_id=None, workflow_id=self.self_heal_id, run_id=values["run_id"],
                                  inputs={"proposal_id": proposal_id}, status="suspended", attempts=1,
                                  created_at=created, finished_at=created + _h(1),
                                  concurrency_key=f"{fixes.JOB_KEY}{proposal_id}")
                values["job_id"] = job_id
                values["gate_id"] = self._one(
                    gates, run_id=values["run_id"], job_id=job_id, project_id=None, step_id="approval",
                    step_key="approval", present=f"Armature proposes a fix for {fixes.pattern_words(label)} in {repo}.",
                    state="open", min_role="team_lead",
                    nonce=_sha("gate", proposal_id) + _sha("nonce", proposal_id)[:24],
                    created_at=created + _h(1), expires_at=created + timedelta(hours=168))
            if state in ("approved", "declined", "expired", "opened", "merged", "closed"):
                values["decided_at"] = created + _h(20)
            if state in ("opened", "merged", "closed"):
                values.update(pr_number=extra["pr"], pr_url=f"{metrics.GITHUB}/{repo}/pull/{extra['pr']}")
            if state == "merged":
                values["merged_at"] = self.now - timedelta(days=extra["merged_days_ago"])
            if state == "declined":
                values["decline_reason"] = extra["reason"]
            if state == "failed":
                values["failure_reason"] = fixes.NOTHING
            self._one(fix_proposals, id=proposal_id, **values)

    def _people(self) -> None:
        now = self.now
        for spec in REPOS:
            repo_id = self.add(gh_repos, gh_repo_id=next(self.gh_ids), full_name=spec.full_name, enabled=True,
                               created_at=now, updated_at=now)
            self.repos[spec.full_name] = Repo(repo_id, spec)
        for name, repo, speed in (*PEOPLE, (BOT[0], ANDROID, 1.0)):
            human = name != BOT[0]
            dev = Dev(new_id(), name, _login(name) if human else name, next(self.gh_ids), repo, speed, human)
            self.add(people, id=dev.id, display_name=name, kind="human" if human else "bot", user_id=None,
                     created_at=now, updated_at=now)
            for kind, value in (("github_id", str(dev.gh)), ("github_login", dev.login)):
                self.add(person_identities, person_id=dev.id, kind=kind, value=value, verified=True,
                         first_seen=now - timedelta(days=HISTORY_DAYS), last_seen=now)
            if human:
                self.devs.append(dev)
            self.by_name[name] = dev

    def _plans(self) -> list[Plan]:
        rng, start, plans = self.rng, self.now - timedelta(days=HISTORY_DAYS), []
        for dev in self.devs:
            for day in range(HISTORY_DAYS):
                date = (start + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
                if rng.random() >= (0.45 if date.weekday() < 5 else 0.04):
                    continue
                created = date + _h(rng.uniform(8, 18.5))
                if created >= self.now - _h(1):
                    continue
                repo = dev.repo if rng.random() < 0.85 else rng.choice([s.full_name for s in REPOS
                                                                       if s.full_name != dev.repo])
                plans.append(self._plan(repo, dev, created))
        bot = self.by_name[BOT[0]]
        for repo in BOT[1]:
            for day in range(3, HISTORY_DAYS, 9):
                created = (start + timedelta(days=day)).replace(hour=6, minute=rng.randint(0, 59), second=0,
                                                                 microsecond=0)
                dep = rng.choice(("okhttp", "coil", "lottie", "firebase-bom", "kingfisher", "swift-collections"))
                lines = rng.randint(2, 12)
                plans.append(Plan(repo, bot, created, 1, metrics.PLATFORM, lines, [self._lockfile(repo)],
                                  f"Update dependency {dep} to v{rng.randint(2, 9)}.{rng.randint(0, 20)}.0"))
        return plans

    def _lockfile(self, repo: str) -> str:
        return "gradle/libs.versions.toml" if repo == ANDROID else "TreeApp.xcodeproj/project.pbxproj"

    def _plan(self, repo: str, dev: Dev, created: datetime) -> Plan:
        rng, spec = self.rng, self.repos[repo].spec
        change = rng.choices([c for c, _ in CHANGE_WEIGHTS], [w for _, w in CHANGE_WEIGHTS])[0]
        low, high = rng.choices(SIZES, UPKEEP_WEIGHTS if change == metrics.PLATFORM else SIZE_WEIGHTS)[0]
        lines = rng.randint(low, high)
        if change == metrics.PLATFORM:
            title = rng.choice(spec.upkeep)
        else:
            title = rng.choice(TITLES[change]).format(area=rng.choice(spec.areas))
            title = title[0].upper() + title[1:]
        count = min(len(spec.files), 1 + lines // 150 + rng.randint(0, 2))
        files = rng.sample(spec.files, count)
        conflict = 0.0
        if rng.random() < spec.conflict_share:
            conflict = rng.uniform(*spec.conflict_hours)
            files = [spec.conflict_file, *files[:2]]
        elif rng.random() < 0.1:
            files.append(spec.conflict_file)
        if repo == ANDROID and created >= self.now - timedelta(days=32):
            rounds = rng.choice((2, 2, 3, 3, 3, 4))
        elif repo == ANDROID:
            rounds = rng.choice((1, 1, 2, 2, 2, 3))
        else:
            rounds = rng.choice((1, 2, 2, 2, 3))
        return Plan(repo, dev, created, rounds, change, lines, files, title, conflict=conflict,
                    closed=rng.random() < 0.04)

    def _stories(self) -> list[Plan]:
        """The pull requests behind each attention item: two dropped flags that became bugs this month and two
        before it, all by the concurrency specialist this month, and one person with two pull requests no one
        reviewed."""
        def at(days: int, hour: int) -> datetime:
            return (self.now - timedelta(days=days)).replace(hour=hour, minute=17, second=0, microsecond=0)

        def plan(name: str, repo: str, days: int, hour: int, rounds: int, change: str, lines: int, files: list[str],
                 title: str, flags: list[Flag] | None = None, **extra: Any) -> Plan:
            return Plan(repo, self.by_name[name], at(days, hour), rounds, change, lines, files, title, flags=flags,
                        **extra)

        sync_broke = plan("Daniel Okafor", ANDROID, 26, 9, 1, "new-feature", 240,
                          [SYNC_WORKER, TREE_CACHE, "app/src/main/java/com/acme/tree/list/TreeListViewModel.kt"],
                          "Sync tree edits in the background with WorkManager", [
                              Flag("concurrency", "concurrency", SYNC_WORKER,
                                   "SyncWorker writes TreeCache on a background thread while TreeListViewModel reads "
                                   "it on the main thread with no lock", "none", severity="high"),
                              Flag("correctness", "error-handling", TREE_CACHE,
                                   "TreeCache.save swallows the IOException and reports success", "kept")])
        sync_fix = plan("Aisha Bello", ANDROID, 12, 14, 2, "bug-fix", 64,
                        [SYNC_WORKER, TREE_CACHE, "app/src/test/java/com/acme/tree/sync/SyncWorkerTest.kt"],
                        "Fix the race between SyncWorker and the tree list cache")
        map_broke = plan("Kenji Watanabe", IOS, 20, 11, 2, "enhancement", 180, [MAP_MODEL, PIN_CLUSTERER],
                         "Cluster map pins in the background", [
                             Flag("concurrency", "concurrency", MAP_MODEL,
                                  "MapViewModel publishes pins from a background task instead of the main actor",
                                  "drop"),
                             Flag("performance", "performance", PIN_CLUSTERER,
                                  "PinClusterer recomputes every cluster on each camera move", "kept", round=1)])
        map_revert = plan("Sofia Lindqvist", IOS, 6, 10, 1, "bug-fix", 180, [MAP_MODEL, PIN_CLUSTERER],
                          'Revert "Cluster map pins in the background"')
        log_broke = plan("Omar Haddad", SERVICE, 70, 10, 2, "new-feature", 120,
                         [HTTP_LOGGING, "internal/export/job.go"], "Log request details for the export job", [
                             Flag("security", "security-privacy", HTTP_LOGGING,
                                  "The request logger writes the Authorization header to the log", "none",
                                  severity="high")])
        log_fix = plan("Grace Mensah", SERVICE, 55, 15, 1, "bug-fix", 22, [HTTP_LOGGING],
                       "Stop logging Authorization headers")
        photo_broke = plan("Mateo Alvarez", ANDROID, 50, 9, 2, "enhancement", 140, [TREE_DETAIL, TREE_CACHE],
                           "Load tree photos from the disk cache first", [
                               Flag("correctness", "crash-safety", TREE_DETAIL,
                                    "cachedPhoto!! crashes when the cache evicts the photo before the fragment "
                                    "resumes", "drop", severity="high")])
        photo_fix = plan("Hannah Kowalski", ANDROID, 41, 13, 1, "bug-fix", 18, [TREE_DETAIL],
                         "Handle an evicted photo in TreeDetailFragment")
        self.links += [
            (sync_fix, sync_broke, "fix", 0.85, {"reason": "names the race and changes the flagged file"}),
            (map_revert, map_broke, "revert", 1.0, {"confirmed": True}),
            (log_fix, log_broke, "fix", 0.75, {"reason": "changes the flagged file"}),
            (photo_fix, photo_broke, "fix", 0.9, {"confirmed": True}),
        ]
        waiting = [
            plan("Hannah Kowalski", ANDROID, 5, 9, 0, "new-feature", 310,
                 ["app/src/main/java/com/acme/tree/list/TreeListScreen.kt",
                  "app/src/main/java/com/acme/tree/species/SpeciesPicker.kt"],
                 "Add species search to the tree list", reviewed=False),
            plan("Hannah Kowalski", ANDROID, 3, 15, 0, "enhancement", 75,
                 ["app/src/main/java/com/acme/tree/list/TreeListScreen.kt"], "Show sync status in the toolbar",
                 reviewed=False),
            plan("Lucas Ferreira", SERVICE, 9, 10, 0, "code-maintenance", 620,
                 ["internal/species/index.go", "internal/search/query.go"], "WIP: shard the species index",
                 reviewed=False, draft=True),
        ]
        return [sync_broke, sync_fix, map_broke, map_revert, log_broke, log_fix, photo_broke, photo_fix, *waiting]

    def _reviewers(self, p: Plan) -> list[Dev]:
        mates = [d for d in self.devs if d.repo == p.repo and d is not p.author]
        others = [d for d in self.devs if d.repo != p.repo and d is not p.author]
        picked = self.rng.sample(mates, 2)
        if self.rng.random() < 0.1:
            picked[1] = self.rng.choice(others)
        return picked

    def _pull_request(self, p: Plan) -> None:
        rng, now = self.rng, self.now
        repo = self.repos[p.repo]
        full = repo.spec.full_name
        heads = (_sha(full, p.number, k) for k in itertools.count())
        head = next(heads)
        pushed: list[tuple[datetime, str, str, str, bool]] = []
        reviews: list[tuple[datetime, Dev, str, str]] = []
        visits: list[tuple[datetime, str, int]] = []
        checks: list[tuple[str, str, datetime, str]] = []
        samples: list[tuple[datetime, str]] = []

        def push(when: datetime, kind: str, armature: bool = False) -> None:
            nonlocal head
            after = next(heads)
            pushed.append((when, head, after, kind, armature))
            head = after

        appeared, merged = p.created, None
        if not p.reviewed:
            if not p.draft:
                visits.append((p.created + timedelta(minutes=rng.uniform(6, 25)), head, 0))
        else:
            reviewers = self._reviewers(p)
            t = p.created + _h(min(20.0, rng.uniform(0.4, 6.0) * reviewers[0].speed))
            for i in range(p.rounds):
                if p.author.human and (p.flags is not None or rng.random() < (0.92 if i == 0 else 0.55)):
                    visits.append((appeared + timedelta(minutes=rng.uniform(6, 25)), head, i))
                last = i == p.rounds - 1
                reviewer = reviewers[0] if i == 0 or rng.random() < 0.7 else reviewers[1]
                state = "approved" if last else rng.choice(("changes_requested", "changes_requested", "commented"))
                reviews.append((t, reviewer, state, head))
                if last:
                    if rng.random() < 0.3:
                        reviews.append((t + _h(rng.uniform(0.2, 3.0)), reviewers[1], "approved", head))
                    break
                if i == 0 and rng.random() < 0.15:
                    checks.append(("unit-tests" if "unit-tests" in repo.spec.checks else "test", head,
                                   t - _h(0.5), "failure"))
                fix_at = t + _h(rng.uniform(1.0, 20.0))
                if rng.random() < 0.2:
                    push(t + (fix_at - t) * rng.uniform(0.1, 0.8),
                         rng.choice((pushes.REBASE, pushes.MERGE_FROM_BASE)))
                roll = rng.random()
                kind = pushes.ARMATURE_FIX if roll < 0.05 else pushes.NEW_SCOPE if roll < 0.15 \
                    else pushes.FORCE_REWRITE if roll < 0.2 else pushes.FIX_AFTER_REVIEW
                push(fix_at, kind, armature=kind == pushes.ARMATURE_FIX)
                appeared = fix_at
                t = fix_at + _h(min(24.0, rng.uniform(0.5, 8.0) * reviewer.speed))
            end = max(r[0] for r in reviews)
            if p.conflict:
                start = end + _h(rng.uniform(0.5, 3.0))
                samples.append((start - _h(rng.uniform(1.0, 3.0)), "mergeable"))
                when = start
                while when < start + _h(p.conflict):
                    samples.append((when, "conflicting"))
                    when += _h(rng.uniform(1.0, 2.0))
                end = start + _h(p.conflict)
                samples.append((end, "conflicting"))
                samples.append((end + _h(0.6), "mergeable"))
                push(end + _h(0.2), rng.choice((pushes.MERGE_FROM_BASE, pushes.MERGE_FROM_BASE, pushes.REBASE)))
                end += _h(0.2)
            done = end
            for name in repo.spec.checks:
                completed = end + timedelta(minutes=rng.uniform(8, 45))
                checks.append((name, head, completed, "failure" if rng.random() < 0.05 else "success"))
                done = max(done, completed)
            merged = done + _h(rng.uniform(0.2, 6.0))

        state, closed_at, merged_at = "open", None, None
        if merged is not None and merged < now:
            if p.closed:
                state, closed_at = "closed", merged
            else:
                state, closed_at, merged_at = "merged", merged, merged
        if p.draft:
            state = "open"
        final = p.created
        before_now = [x for x in pushed if x[0] < now]
        head_now = before_now[-1][2] if before_now else _sha(full, p.number, 0)
        for when, _, _, _, _ in before_now:
            final = max(final, when)
        self.add(gh_pull_requests, id=p.id, repo_row_id=repo.id, gh_pr_id=next(self.gh_ids), number=p.number,
                 title=p.title, author_person_id=p.author.id, author_gh_id=p.author.gh, author_login=p.author.login,
                 state=state, draft=p.draft, base_ref="main",
                 head_ref=f"{p.author.login.removesuffix('[bot]')}/{_login(p.title)[:40]}", head_sha=head_now,
                 base_sha=_sha(full, "main", p.number), merge_commit_sha=_sha(full, p.number, "merge")
                 if merged_at else None, merge_method="squash" if merged_at else None,
                 additions=round(p.lines * 0.7), deletions=p.lines - round(p.lines * 0.7),
                 changed_files=len(p.files), created_at_gh=p.created, closed_at_gh=closed_at, merged_at_gh=merged_at,
                 merged_by_gh_id=p.author.gh if merged_at else None,
                 gh_updated_at=closed_at or final, created_at=now, updated_at=now)
        if p.change_type:
            self._label(metrics.CHANGE_KIND, p.id, p.change_type, p.created)
        for when, before, after, kind, armature in before_now:
            pusher = self._armature() if armature else p.author
            self.add(gh_pushes, pr_row_id=p.id, before_sha=before, after_sha=after, base_sha=None,
                     forced=kind in (pushes.REBASE, pushes.FORCE_REWRITE), pusher_gh_id=pusher.gh,
                     pusher_login=pusher.login, pusher_person_id=pusher.id,
                     actor_source="armature" if armature else "github", pushed_at=when, source="sync",
                     push_class=kind, class_confidence=round(rng.uniform(0.6, 0.97), 2),
                     class_evidence={"demo": True}, classifier_version=pushes.CLASSIFIER_VERSION, created_at=now)
        for when, reviewer, review_state, reviewed_head in reviews:
            if when < now:
                self._review(p, full, when, reviewer, review_state, reviewed_head)
        for name, checked, completed, conclusion in checks:
            started = completed - timedelta(minutes=rng.uniform(4, 30))
            if started >= now:
                continue
            finished = completed < now
            self.add(gh_check_runs, pr_row_id=p.id, gh_check_run_id=next(self.gh_ids), repo_row_id=repo.id,
                     name=name, head_sha=checked, status="completed" if finished else "in_progress",
                     conclusion=conclusion if finished else None, started_at=started,
                     completed_at=completed if finished else None, created_at=started)
        for when, mergeable in samples:
            if when < now:
                self.add(gh_conflict_samples, pr_row_id=p.id, sampled_at=when, mergeable=mergeable,
                         merge_state="dirty" if mergeable == "conflicting" else "clean")
        if merged_at:
            for path in p.files:
                self.add(gh_pr_files, repo_row_id=repo.id, pr_row_id=p.id, path=path,
                         path_key=hashlib.sha256(path.encode()).hexdigest(), created_at=now)
        for when, visited, i in visits:
            if when < now:
                self._run(p, full, when, visited, i)

    def _armature(self) -> Dev:
        return Dev(self.armature_id, "Armature", ARMATURE_LOGIN, 9_001_001, "", 1.0, human=False)

    def _review(self, p: Plan, full: str, when: datetime, reviewer: Dev, state: str, head: str) -> None:
        rng = self.rng
        review_id = next(self.gh_ids)
        count = rng.randint(1, 3) if state != "approved" else rng.choice((0, 0, 0, 1))
        self.add(gh_reviews, pr_row_id=p.id, gh_review_id=review_id, reviewer_person_id=reviewer.id,
                 reviewer_gh_id=reviewer.gh, reviewer_login=reviewer.login, actor_source="github", state=state,
                 head_sha=head, submitted_at=when, body_length=rng.randint(0, 400), comment_count=count,
                 url=f"{metrics.GITHUB}/{full}/pull/{p.number}#pullrequestreview-{review_id}", created_at=when,
                 updated_at=when)
        started = when - timedelta(minutes=rng.uniform(5, 50))
        for k in range(count):
            at = started + (when - started) * (k / max(count, 1))
            path = rng.choice(p.files)
            row_id, comment_id = self._comment(p, full, reviewer, path, at, review_id, None, head)
            self._label(metrics.COMMENT_KIND, row_id,
                        metrics.SUBSTANTIVE if rng.random() < (0.6 if state != "approved" else 0.2) else "nit", at)
            reply = at + _h(rng.uniform(0.5, 6.0))
            if rng.random() < 0.3 and reply < self.now:
                self._comment(p, full, p.author, path, reply, None, comment_id, head)

    def _comment(self, p: Plan, full: str, who: Dev, path: str, at: datetime, review_id: int | None,
                 reply_to: int | None, head: str) -> tuple[str, int]:
        comment_id = next(self.gh_ids)
        return self.add(gh_review_comments, pr_row_id=p.id, gh_comment_id=comment_id, gh_review_id=review_id,
                        author_person_id=who.id, author_gh_id=who.gh, author_login=who.login, path=path,
                        line=self.rng.randint(8, 360), head_sha=head, created_at_gh=at, updated_at_gh=at,
                        body_length=self.rng.randint(20, 600),
                        url=f"{metrics.GITHUB}/{full}/pull/{p.number}#discussion_r{comment_id}", in_reply_to=reply_to,
                        created_at=at), comment_id

    def _label(self, kind: str, subject_id: str, label: str, at: datetime) -> None:
        self.add(labels, subject_kind=kind, subject_id=subject_id, taxonomy_version=taxonomy.VERSION, label=label,
                 confidence=round(self.rng.uniform(0.7, 0.98), 2), labeler=DEMO_ACTOR, created_at=at)

    def _flags(self, p: Plan, full: str, i: int) -> list[Flag]:
        if p.flags is not None:
            return [f for f in p.flags if f.round == i]
        rng, found = self.rng, []
        late = p.created >= self.now - timedelta(days=32)
        for specialist in self.repos[p.repo].spec.specialists:
            chance = 0.5 if full == ANDROID and late and i > 0 and specialist == "concurrency" else 0.17
            if rng.random() >= chance:
                continue
            category = rng.choice(SPECIALISTS[specialist])
            path = rng.choice(p.files)
            status = rng.choices([s for s, _ in STATUS_WEIGHTS], [w for _, w in STATUS_WEIGHTS])[0]
            found.append(Flag(specialist, category if rng.random() < 0.93 else None, path,
                              rng.choice(CLAIMS[category]).format(name=_name(path)), status, i,
                              rng.choices(("high", "medium", "low"), (15, 45, 40))[0]))
        return found

    def _run(self, p: Plan, full: str, at: datetime, head: str, i: int) -> None:
        rng = self.rng
        target = f"{full}#{p.number}"
        run_id = new_id()
        calls, cost, tokens_in, tokens_out = [], 0.0, 0, 0
        for seq, specialist in enumerate((*self.repos[p.repo].spec.specialists, "lead"), start=1):
            model, provider, price_in, price_out = MODELS[specialist]
            used_in = int(rng.uniform(7000, 16000) + min(p.lines, 1500) * 45) * (2 if specialist == "lead" else 1)
            used_out = rng.randint(350, 2400)
            spent = round((used_in * price_in + used_out * price_out) / 1_000_000, 6)
            calls.append((seq, specialist, model, provider, used_in, used_out, spent))
            cost, tokens_in, tokens_out = cost + spent, tokens_in + used_in, tokens_out + used_out
        started = at - timedelta(minutes=rng.uniform(3, 9))
        self.add(runs, id=run_id, project_id=None, workflow_name="pr-review", status="succeeded",
                 trigger_kind="webhook", actor=DEMO_ACTOR, started_at=started, finished_at=at,
                 cost_usd=round(cost, 6), input_tokens=tokens_in, output_tokens=tokens_out, subject=target,
                 head_sha=head)
        for seq, specialist, model, provider, used_in, used_out, spent in calls:
            self.add(llm_calls, run_id=run_id, seq=seq, step_key=f"review/{specialist}", model_id=model,
                     provider=provider, input_tokens=used_in, output_tokens=used_out, cost_usd=spent, cached=False,
                     created_at=started + (at - started) * (seq / (len(calls) + 1)))
        flags = self._flags(p, full, i)
        blocking = False
        ordinals: dict[str, int] = defaultdict(int)
        for flag in flags:
            ordinal = ordinals[flag.specialist]
            ordinals[flag.specialist] += 1
            candidate = self.add(finding_candidates, project_id=None, run_id=run_id,
                                 step_key=f"review/{flag.specialist}", specialist=flag.specialist, ordinal=ordinal,
                                 path=flag.path, line=rng.randint(12, 380), severity=flag.severity,
                                 blocking=flag.severity == "high", claim=flag.claim, created_at=at)
            finding_id = None
            if flag.status != "none":
                decision = {"drop": "drop", "fixed": "fixed"}.get(flag.status) or \
                    ("block" if flag.severity == "high" else "suggest")
                blocking = blocking or decision == "block"
                finding_id = self.add(findings, run_id=run_id, project_id=None, finding_key=_sha(candidate)[:12],
                                      path=flag.path, severity=flag.severity, title=flag.claim, decision=decision,
                                      created_at=at)
            self.add(candidate_matches, candidate_id=candidate, finding_id=finding_id, match_ordinal=0,
                     method="key" if finding_id else "none", score=1.0 if finding_id else None,
                     matcher_version=MATCHER_VERSION, created_at=at)
            if flag.category:
                self._label("candidate", candidate, flag.category, at)
        review_id = next(self.gh_ids)
        arm = self._armature()
        self.add(gh_reviews, pr_row_id=p.id, gh_review_id=review_id, reviewer_person_id=arm.id, reviewer_gh_id=arm.gh,
                 reviewer_login=arm.login, actor_source="armature",
                 state="changes_requested" if blocking else "commented", head_sha=head, submitted_at=at,
                 body_length=rng.randint(200, 1800), comment_count=len(flags),
                 url=f"{metrics.GITHUB}/{full}/pull/{p.number}#pullrequestreview-{review_id}", created_at=at,
                 updated_at=at)
        for tool, step, detail in (("review.decisions", "decisions", None),
                                   ("github.post-review", "post", {"posted": True, "head": head,
                                                                   "review_id": review_id})):
            self.add(tool_calls, run_id=run_id, project_id=None, step_id=step, tool=tool, mode="live", outcome="ok",
                     target=target, detail=detail, created_at=at)


INSERT_ORDER = (gh_repos, people, person_identities, gh_pull_requests, gh_pushes, gh_reviews, gh_review_comments,
                gh_check_runs, gh_conflict_samples, gh_pr_files, gh_pr_links, runs, tool_calls, findings,
                finding_candidates, candidate_matches, labels, llm_calls)


def _demo_team(conn: Connection, slug: str, now: datetime, force: bool) -> str:
    """The demo team's ID, made and marked when no team has the slug. Raises Refused before writing anything."""
    if not SLUG.fullmatch(slug):
        raise Refused(f"{slug!r} isn't a slug: use up to 40 lowercase letters, digits, and dashes.")
    demo = set(conn.execute(select(settings.c.team_id).where(settings.c.key == DEMO_SETTING,
                                                             settings.c.team_id.is_not(None),
                                                             settings.c.project_id.is_(None))).scalars())
    every = conn.execute(select(teams.c.id, teams.c.name, teams.c.slug)).all()
    found = conn.execute(select(teams.c.id).where(or_(teams.c.slug == slug, teams.c.name == slug))).scalars().all()
    if len(found) > 1:
        raise Refused(f"{slug} is the slug of one team and the name of another, so it could mean either.")
    if found and found[0] not in demo:
        raise Refused(f"Team {slug} isn't a demo team, so it gets no invented history. Pick a slug no team has.")
    others = sorted(t.name for t in every if t.id not in demo)
    if others and not force:
        raise Refused(f"This database holds teams that aren't demo teams ({', '.join(others)}). Seed a scratch "
                      f"database, or pass --force to seed beside them.")
    if found:
        conn.execute(update(settings).where(settings.c.team_id == found[0], settings.c.project_id.is_(None),
                                            settings.c.key == DEMO_SETTING)
                     .values(value={"seeded_at": now.isoformat()}, updated_at=now))
        return found[0]
    team_id = new_id()
    conn.execute(insert(teams).values(id=team_id, name=slug, slug=slug, created_at=now))
    conn.execute(insert(settings).values(team_id=team_id, key=DEMO_SETTING, value={"seeded_at": now.isoformat()},
                                         updated_at=now))
    audit.record(conn, action="team.create", actor_id=None, team_id=team_id, target_kind="team", target_id=team_id,
                 detail={"source": DEMO_ACTOR, "slug": slug})
    return team_id


def _clear(conn: Connection, team_id: str) -> None:
    """Remove the demo team's invented history: its GitHub rows, people, labels, clusters, and the demo's runs."""
    demo_runs = select(runs.c.id).where(runs.c.team_id == team_id, runs.c.actor == DEMO_ACTOR)
    candidates = select(finding_candidates.c.id).where(finding_candidates.c.run_id.in_(demo_runs))
    conn.execute(delete(candidate_matches).where(candidate_matches.c.candidate_id.in_(candidates)))
    conn.execute(delete(fix_proposals).where(fix_proposals.c.team_id == team_id))
    for table in (gates, jobs, findings, finding_candidates, llm_calls, tool_calls):
        conn.execute(delete(table).where(table.c.run_id.in_(demo_runs)))
    conn.execute(delete(runs).where(runs.c.team_id == team_id, runs.c.actor == DEMO_ACTOR))
    for table in (cluster_members, clusters, labels, gh_pr_files, gh_pr_links, gh_conflict_samples, gh_check_runs,
                  gh_review_comments, gh_reviews, gh_pushes, gh_pr_commits, gh_sync_state, gh_pull_requests, gh_repos,
                  person_identities, people):
        conn.execute(delete(table).where(table.c.team_id == team_id))


def seed(engine: Engine, slug: str, now: datetime, *, force: bool = False) -> dict[str, int]:
    """Replace the demo team's history with HISTORY_DAYS of invented history ending at ``now``, making the team
    when no team has ``slug``. Returns how many rows went into the main tables."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    with engine.begin() as conn:
        team_id = _demo_team(conn, slug, now, force)
        _clear(conn, team_id)
        taxonomy.ensure(conn, team_id, now)
        armature_id = attribution.armature_person(conn, team_id, now)
        self_heal = conversations.builtin_workflow(conn, fixes.SELF_HEAL, now, kind="fix")
        history = History(team_id, now, armature_id, self_heal)
        rows = history.build()
        for table in INSERT_ORDER:
            for start in range(0, len(rows[table]), CHUNK):
                conn.execute(insert(table), rows[table][start:start + CHUNK])
        for table, row in history.fix_rows:
            conn.execute(insert(table).values(**row))
        # The flags before a merged fix come from the invented history itself, as fixes.track would count them.
        for row in conn.execute(select(fix_proposals).where(fix_proposals.c.team_id == team_id,
                                                            fix_proposals.c.state == "merged")).all():
            conn.execute(update(fix_proposals).where(fix_proposals.c.id == row.id).values(
                flags_before=fixes.flags(conn, team_id, row.evidence, row.merged_at - fixes.BEFORE, row.merged_at)))
    return {"pull_requests": len(rows[gh_pull_requests]), "reviews": len(rows[gh_reviews]),
            "people": len(rows[people]), "flags": len(rows[finding_candidates]), "runs": len(rows[runs]),
            "llm_calls": len(rows[llm_calls])}
