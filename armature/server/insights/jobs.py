"""The daily labeling of findings, review comments, and change types, the weekly clustering of findings, the weekly
model recommendations, and the weekly fix proposals, run per team by a daemon like the trigger scheduler, plus the
instance-wide daily report.

The daily report's row is its own done marker, so a failed build retries on the next tick. Each team's next run of
each job is a settings row. A server claims a due slot by moving its next run forward with a
compare-and-set on updated_at, so two servers never run one team's job at once. Tests call tick() themselves.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import broker, display, llm
from armature.server.config import ReportsConfig
from armature.server.crypto import Keyring
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    cluster_members,
    clusters,
    daily_reports,
    finding_candidates,
    labels,
    settings,
    teams,
)
from armature.server.insights import (
    change_types,
    comment_labels,
    fixes,
    labeling,
    metrics,
    proposals,
    report,
    report_file,
    scoring,
    taxonomy,
)

log = logging.getLogger(__name__)

TICK_S = 15 * 60
STOP_TIMEOUT_S = 5.0
# Internal state, never listed with the settings people edit.
LABEL_SLOT = "_insights.jobs.label"
CLUSTER_SLOT = "_insights.jobs.cluster"
RECOMMEND_SLOT = "_insights.jobs.recommend"
FIX_SLOT = "_insights.jobs.fixes"
SLOTS = (LABEL_SLOT, CLUSTER_SLOT, RECOMMEND_SLOT, FIX_SLOT)
# One slot per experiment, which the comparison runs daemon claims each tick.
ARM_SLOT_PREFIX = "_insights.arms."
CAP_SETTING = "insights.label_daily_cap"
DAILY_CAP = 2000
CLUSTER_SPAN = timedelta(days=28)
CLUSTER_MIN = 3


def next_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def next_monday(now: datetime) -> datetime:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=7 - midnight.weekday())


def claim(engine: Engine, team_id: str, slot: str, now: datetime, until: datetime) -> bool:
    """Whether this server took the team's due slot, moving its next run to ``until``."""
    with engine.begin() as conn:
        row = conn.execute(select(settings.c.value, settings.c.updated_at).where(
            settings.c.scope == team_id, settings.c.key == slot)).first()
        value = {"next_run_at": until.isoformat()}
        if row is None:
            try:
                with conn.begin_nested():
                    conn.execute(insert(settings).values(team_id=team_id, scope=team_id, key=slot, value=value,
                                                         updated_at=now))
                return True
            except IntegrityError:
                return False
        due = (row.value or {}).get("next_run_at") if isinstance(row.value, dict) else None
        if isinstance(due, str) and datetime.fromisoformat(due) > now:
            return False
        return take(conn, team_id, slot, row.updated_at, until, now)


def take(conn: Connection, team_id: str, slot: str, seen: datetime | None, until: datetime, now: datetime) -> bool:
    """Move the slot to ``until`` only if nobody moved it since this server read ``seen``."""
    stamp = settings.c.updated_at.is_(None) if seen is None else settings.c.updated_at == seen
    return conn.execute(update(settings).where(settings.c.scope == team_id, settings.c.key == slot, stamp)
                        .values(value={"next_run_at": until.isoformat()}, updated_at=now)).rowcount == 1


def daily_cap(engine: Engine, team_id: str) -> int:
    with engine.connect() as conn:
        value = broker.setting(conn, CAP_SETTING, team_id, None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else DAILY_CAP


def label_team(engine: Engine, keyring: Keyring, team_id: str, now: datetime,
               transport: httpx.BaseTransport | None = None) -> dict[str, labeling.Totals]:
    """Label the team's findings, then review comments, then merged pull requests' change types, all within one
    daily cap. Returns the totals of each pass that ran, by name.

    Without a model, or once a model call fails, only Jira issue types label anything. Each pass audits what it spent,
    as label-findings does.
    """
    left = daily_cap(engine, team_id)
    if not left:
        return {}
    with engine.connect() as conn:
        try:
            alias: str | None = labeling.default_alias(conn, team_id)[0]
        except llm.LLMError as exc:
            log.warning("labeling for team %s has no model, so only Jira issue types label: %s", team_id, exc.message)
            alias = None
        findings = bool(labeling._unlabeled(conn, team_id, 1))
    # Each pass: its name, whether it needs a model, whether it has work, and how to run it.
    passes: tuple[tuple[str, bool, bool, Callable[..., Any]], ...] = (
        ("findings", True, findings, labeling.label),
        ("comments", True, True, partial(comment_labels.label, transport=transport)),
        ("changes", False, True, partial(change_types.label, transport=transport)),
    )
    done: dict[str, labeling.Totals] = {}
    for name, needs_model, has_work, run in passes:
        if left <= 0 or not has_work or (needs_model and alias is None):
            continue
        totals = done[name] = labeling.Totals()
        try:
            run(engine, keyring, team_id, alias, limit=left, now=now, totals=totals)
        except llm.LLMError as exc:
            log.warning("labeling %s for team %s stopped, so only Jira issue types label: %s", name, team_id,
                        exc.message)
            alias = None
        left -= totals.attempted
    return done


def _area(path: str | None) -> str | None:
    """The top two directories of a path."""
    if not path:
        return None
    folders = [part for part in path.split("/")[:-1] if part]
    return "/".join(folders[:2]) or None


def _summary(label: str, repo: str | None, area: str | None, size: int) -> str:
    where = " ".join(part for part in (repo.rpartition("/")[2] if repo else None, area) if part)
    name = label.replace("-", " ").capitalize()
    return f"{name}{f' in {where}' if where else ''}, flagged {size} times in {CLUSTER_SPAN.days // 7} weeks"


def cluster(engine: Engine, team_id: str, now: datetime) -> dict[str, int]:
    """Group the last 28 days' labeled flags by category, repository, and area, and store each group of at least
    CLUSTER_MIN as a cluster whose members are exactly that group. Returns how many clusters it stored."""
    since = now - CLUSTER_SPAN
    fc = finding_candidates.c
    with engine.begin() as conn:
        rows = conn.execute(select(fc.id, fc.run_id, fc.path, fc.created_at, labels.c.label)
                            .join(labels, (labels.c.subject_id == fc.id) & (labels.c.team_id == fc.team_id))
                            .where(fc.team_id == team_id, fc.arm_id.is_(None), fc.created_at >= since,
                                   fc.created_at < now,
                                   labels.c.subject_kind == "candidate",
                                   labels.c.taxonomy_version == taxonomy.VERSION)).all()
        targets = metrics.run_targets(conn, team_id, {r.run_id for r in rows}) if rows else {}
        groups: dict[tuple[str, str | None, str | None], list[Any]] = defaultdict(list)
        for r in rows:
            repo = targets.get(r.run_id, "").rpartition("#")[0] or None
            groups[(r.label, repo, _area(r.path))].append(r)
        stored = conn.execute(select(clusters).where(clusters.c.team_id == team_id,
                                                     clusters.c.taxonomy_version == taxonomy.VERSION)).all()
        known = {(c.label, (c.scope or {}).get("repo"), (c.scope or {}).get("area")): c for c in stored}
        kept = 0
        # A stored cluster whose group shrank below the minimum, or went away, goes too.
        gone = [c.id for key, c in known.items() if len(groups.get(key, ())) < CLUSTER_MIN]
        if gone:
            conn.execute(delete(cluster_members).where(cluster_members.c.team_id == team_id,
                                                       cluster_members.c.cluster_id.in_(gone)))
            conn.execute(delete(labels).where(labels.c.team_id == team_id, labels.c.subject_kind == "cluster",
                                              labels.c.subject_id.in_(gone)))
            conn.execute(delete(clusters).where(clusters.c.team_id == team_id, clusters.c.id.in_(gone)))
        for key, members in groups.items():
            if len(members) < CLUSTER_MIN:
                continue
            kept += 1
            label, repo, area = key
            first, last = min(m.created_at for m in members), max(m.created_at for m in members)
            values = {"size": len(members), "last_seen": last, "summary": _summary(label, repo, area, len(members))}
            found = known.get(key)
            if found is None:
                cluster_id = new_id()
                conn.execute(insert(clusters).values(id=cluster_id, team_id=team_id,
                                                     taxonomy_version=taxonomy.VERSION, label=label,
                                                     scope={"repo": repo, "area": area}, first_seen=first,
                                                     created_at=now, **values))
            else:
                cluster_id = found.id
                earliest = min(first, found.first_seen) if found.first_seen else first
                conn.execute(update(clusters).where(clusters.c.id == cluster_id).values(first_seen=earliest,
                                                                                        **values))
            ids = {m.id for m in members}
            conn.execute(delete(cluster_members).where(cluster_members.c.cluster_id == cluster_id,
                                                       cluster_members.c.subject_kind == "candidate",
                                                       cluster_members.c.subject_id.not_in(ids)))
            have = set(conn.execute(select(cluster_members.c.subject_id).where(
                cluster_members.c.cluster_id == cluster_id, cluster_members.c.subject_kind == "candidate")).scalars())
            new = [{"id": new_id(), "team_id": team_id, "cluster_id": cluster_id, "subject_kind": "candidate",
                    "subject_id": i, "created_at": now} for i in sorted(ids - have)]
            if new:
                conn.execute(insert(cluster_members), new)
    return {"clusters": kept, "removed": len(gone), "labeled": len(rows)}


def daily_report(engine: Engine, cfg: ReportsConfig, now: datetime) -> date | None:
    """Build and store the oldest day after the newest stored report, up to report.CATCH_UP days back, once that
    day has been over for report.MARGIN, then write its file. With no report yet, only the previous local day. Returns the day it stored, or None when it was early, already stored, or another server stored it first."""
    with engine.connect() as conn:
        zone = ZoneInfo(display.load(conn)["timezone"])
        newest = conn.execute(select(func.max(daily_reports.c.day))).scalar()
        days = [report.target_day(now, zone)] if newest is None else [
            d for d in report.candidate_days(now, zone) if d > newest]
        days = [d for d in days if report.due(now, d, zone)]
        if not days:
            return None
        day = days[0]
        data = report.build(conn, day, cfg)
    try:
        with engine.begin() as conn, conn.begin_nested():
            conn.execute(insert(daily_reports).values(id=new_id(), day=day, data=data, created_at=now))
    except IntegrityError:
        return None
    try:
        report_file.write(cfg.dir, day, report_file.render(data))
    except (OSError, report_file.Unsafe) as exc:
        log.error("the daily report for %s is stored but its file wasn't written: %s", day, exc)
    except Exception as exc:  # the row is stored, so a render bug must still say which day lost its file
        log.error("the daily report for %s is stored but its file wasn't rendered: %s", day, type(exc).__name__)
    return day


class Jobs:
    """Every ``interval`` seconds, builds the daily report when it's due, then runs each team's due labeling,
    clustering, recommendation, and fix slots in turn."""

    def __init__(self, engine: Engine, keyring: Keyring, *, clock: Callable[[], datetime] = utcnow,
                 interval: float = TICK_S, transport: Callable[[], httpx.BaseTransport | None] = lambda: None,
                 reports: ReportsConfig | None = None):
        self.engine = engine
        self.keyring = keyring
        self.reports = reports or ReportsConfig()
        self.interval = interval
        self._clock = clock
        self._transport = transport
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="armature-insights-jobs", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("insights jobs tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        try:
            daily_report(self.engine, self.reports, self._clock())
        except Exception as exc:
            log.error("the daily report failed: %s", type(exc).__name__)
        with self.engine.connect() as conn:
            team_ids = list(conn.execute(select(teams.c.id).order_by(teams.c.id)).scalars())
        for team_id in team_ids:
            if self._stop.is_set():
                return
            now = self._clock()
            try:
                if claim(self.engine, team_id, LABEL_SLOT, now, next_day(now)):
                    label_team(self.engine, self.keyring, team_id, now, self._transport())
                if claim(self.engine, team_id, CLUSTER_SLOT, now, next_monday(now)):
                    cluster(self.engine, team_id, now)
                if claim(self.engine, team_id, RECOMMEND_SLOT, now, next_monday(now)):
                    scoring.recommend(self.engine, team_id, now)
                if claim(self.engine, team_id, FIX_SLOT, now, next_monday(now)):
                    proposals.propose(self.engine, team_id, now)
                with self.engine.begin() as conn:
                    fixes.reconcile(conn, team_id, now)
                fixes.sweep(self.engine, self.keyring, team_id, now, self._transport())
            except Exception as exc:
                log.error("insights jobs for team %s failed: %s", team_id, type(exc).__name__)
