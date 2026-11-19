"""The Insights screens' read-only API, and a team lead's corrections to people. Every route is team-scoped."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import Connection, select

from armature.server import defs, llm, rbac, risk
from armature.server.app.deps import Granted, Server, by_team, require, server
from armature.server.app.http import ApiError, client_ip
from armature.server.db.schema import (
    eval_experiments,
    fix_proposals,
    gates,
    gh_repos,
    people,
)
from armature.server.insights import (
    experiments,
    fixes,
    metrics,
    people_admin,
    recommendations,
    scoring,
    taxonomy,
)

router = APIRouter()

BASE = "/api/teams/{team_id}/insights"
LEAD = "team_lead"
NAME_MAX = 64
LOGIN_MAX = 191
PATH_MAX = 1024
CACHE_TTL_S = 300.0
CACHE_SIZE = 256


class Answers:
    """Computed answers kept for ``ttl`` seconds, at most ``size`` of them, least recently used out first. Concurrent
    requests for one key compute it once. Callers check access before asking, since a hit skips the computation."""

    def __init__(self, ttl: float = CACHE_TTL_S, size: int = CACHE_SIZE,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl, self._size, self._clock = ttl, size, clock
        self._entries: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()
        self._locks: dict[tuple[Any, ...], threading.Lock] = {}
        self._guard = threading.Lock()

    def _fresh(self, key: tuple[Any, ...]) -> tuple[float, Any] | None:
        found = self._entries.get(key)
        if found is None or found[0] <= self._clock():
            return None
        self._entries.move_to_end(key)
        return found

    def get(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
        with self._guard:
            hit = self._fresh(key)
            if hit is not None:
                return hit[1]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                hit = self._fresh(key)
                if hit is not None:
                    return hit[1]
            try:
                value = compute()
            except BaseException:
                with self._guard:
                    self._locks.pop(key, None)
                raise
            with self._guard:
                self._entries[key] = (self._clock() + self._ttl, value)
                self._entries.move_to_end(key)
                while len(self._entries) > self._size:
                    evicted, _ = self._entries.popitem(last=False)
                    self._locks.pop(evicted, None)
            return value

    def forget_team(self, team_id: str) -> None:
        with self._guard:
            for key in [k for k in self._entries if k[0] == team_id]:
                del self._entries[key]
                self._locks.pop(key, None)

    def clear(self) -> None:
        with self._guard:
            self._entries.clear()
            self._locks.clear()


answers = Answers()


def _cached(team_id: str, route: str, params: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
    return answers.get((team_id, route, params), compute)


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except (people_admin.PeopleError, experiments.ExperimentError, recommendations.RecommendationError,
            defs.DefError) as exc:
        raise ApiError(exc.status, exc.message) from exc


def _days(days: str) -> int:
    if not days.isdecimal() or int(days) not in metrics.DAYS:
        raise ApiError(400, "days must be 7, 30, or 90.")
    return int(days)


def _repo(conn: Connection, team_id: str, repo: str | None, status: int = 400) -> str | None:
    if not repo:
        return None
    if conn.execute(select(gh_repos.c.id).where(gh_repos.c.team_id == team_id, gh_repos.c.id == repo)).first() is None:
        raise ApiError(status, "No such tracked repository in this team.")
    return repo


def _person(conn: Connection, team_id: str, person_id: str | None) -> str | None:
    if not person_id:
        return None
    if conn.execute(select(people.c.id).where(people.c.team_id == team_id, people.c.id == person_id)).first() is None:
        raise ApiError(404, "No such person.")
    return person_id


def _lead(granted: Granted) -> bool:
    return rbac.at_least(granted.role, LEAD)


def _read(srv: Server, team_id: str, route: str, params: tuple[Any, ...], compute: Callable[[Connection], Any]) -> Any:
    def run() -> Any:
        with srv.engine.connect() as conn:
            return compute(conn)
    return _cached(team_id, route, params, run)


@router.get(f"{BASE}/overview")
def get_overview(request: Request, days: str = Query("30", max_length=3), repo: str | None = Query(None, max_length=36),
                 granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, team_id, chosen, lead = server(request), granted.target.team_id, _days(days), _lead(granted)
    with srv.engine.connect() as conn:
        repo_id = _repo(conn, team_id, repo)
    return _read(srv, team_id, "overview", (chosen, repo_id, lead), lambda conn: metrics.overview(
        conn, team_id, srv.now(), chosen, repo_id, with_people=lead))


@router.get(f"{BASE}/repos")
def get_repos(request: Request, granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    with server(request).engine.connect() as conn:
        return {"repos": metrics.repos_list(conn, granted.target.team_id)}


@router.get(BASE + "/repos/{repo_id}")
def get_repo(repo_id: str, request: Request, days: str = Query("30", max_length=3),
             granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, team_id, chosen = server(request), granted.target.team_id, _days(days)
    with srv.engine.connect() as conn:
        _repo(conn, team_id, repo_id, 404)
    return _read(srv, team_id, "repo", (chosen, repo_id), lambda conn: metrics.repo_view(
        conn, team_id, srv.now(), chosen, repo_id))


@router.get(f"{BASE}/people")
def get_people(request: Request, days: str = Query("30", max_length=3), repo: str | None = Query(None, max_length=36),
               view: str = Query("shipping", max_length=16),
               granted: Granted = Depends(require(LEAD, by_team()))) -> dict[str, Any]:
    if view not in metrics.VIEWS:
        raise ApiError(400, f"view must be one of {', '.join(metrics.VIEWS)}.")
    srv, team_id, chosen = server(request), granted.target.team_id, _days(days)
    with srv.engine.connect() as conn:
        repo_id = _repo(conn, team_id, repo)
    return _read(srv, team_id, "people", (chosen, repo_id, view), lambda conn: metrics.people_view(
        conn, team_id, srv.now(), chosen, repo_id, view))


def _person_page(srv: Server, granted: Granted, person_id: str | None, chosen: int) -> dict[str, Any]:
    team_id = granted.target.team_id
    with srv.engine.connect() as conn:
        row = conn.execute(select(people).where(people.c.team_id == team_id, people.c.id == person_id)).first() \
            if person_id else None
    # Someone who may only see their own page can't learn whether another person exists.
    if not _lead(granted) and (row is None or row.user_id is None or row.user_id != granted.user_id):
        raise ApiError(403, "Not allowed.")
    if row is None or row.kind != "human":
        raise ApiError(404, "No such person.")
    return _read(srv, team_id, "person", (chosen, row.id), lambda conn: metrics.person_view(
        conn, team_id, srv.now(), chosen, row.id))


@router.get(BASE + "/people/by-login/{login}")
def get_person_by_login(login: str, request: Request, days: str = Query("30", max_length=3),
                        granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, chosen = server(request), _days(days)
    if len(login) > LOGIN_MAX:
        raise ApiError(404, "No such person.")
    with srv.engine.connect() as conn:
        person_id = metrics.person_by_login(conn, granted.target.team_id, login)
    return _person_page(srv, granted, person_id, chosen)


@router.get(BASE + "/people/{person_id}")
def get_person(person_id: str, request: Request, days: str = Query("30", max_length=3),
               granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    return _person_page(server(request), granted, person_id, _days(days))


@router.get(f"{BASE}/findings")
def get_findings(request: Request, days: str = Query("30", max_length=3),
                 repo: str | None = Query(None, max_length=36), category: str | None = Query(None, max_length=64),
                 person: str | None = Query(None, max_length=36),
                 granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    if category and category not in taxonomy.CATEGORIES:
        raise ApiError(400, "No such category.")
    if person and not _lead(granted):
        raise ApiError(403, "Not allowed.")
    srv, team_id, chosen = server(request), granted.target.team_id, _days(days)
    with srv.engine.connect() as conn:
        repo_id, person_id = _repo(conn, team_id, repo), _person(conn, team_id, person)
    return _read(srv, team_id, "findings", (chosen, repo_id, category or None, person_id),
                 lambda conn: metrics.findings_screen(conn, team_id, srv.now(), chosen, repo_id=repo_id,
                                                      category=category or None, person_id=person_id))


@router.get(f"{BASE}/models")
def get_models(request: Request, days: str = Query("30", max_length=3),
               specialist: str | None = Query(None, max_length=NAME_MAX),
               granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, team_id, chosen = server(request), granted.target.team_id, _days(days)

    def compute(conn: Connection) -> dict[str, Any]:
        w = metrics.window(srv.now(), chosen)
        return {"scope": metrics.scope(w, chosen, None), "specialist": specialist or None,
                **metrics.models_view(conn, team_id, w, specialist or None)}
    found = _read(srv, team_id, "models", (chosen, specialist or None), compute)
    # The cached answer is the team's, so whether this viewer may approve is added per request.
    lead, admin = _lead(granted), granted.access.is_admin
    return {**found, "recommendations": [
        {**item, "can_approve": scoring.can_approve(item["change"], lead=lead, admin=admin)}
        for item in found["recommendations"]]}


@router.get(f"{BASE}/fixes")
def get_fixes(request: Request, status: str | None = Query(None, max_length=16),
              granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    if status and status not in metrics.FIX_STATUSES:
        raise ApiError(400, f"status must be one of {', '.join(metrics.FIX_STATUSES)}.")
    srv, team_id = server(request), granted.target.team_id
    # Not cached: a fix moves on as its run and gate do, and only someone who may decide sees the gate's nonce.
    with srv.engine.begin() as conn:
        fixes.reconcile(conn, team_id, srv.now())
        found = metrics.fixes_view(conn, team_id, srv.now())
        ids = [item["gate_id"] for item in found["waiting"] if item["gate_id"]]
        held = {g.id: g for g in conn.execute(select(gates).where(gates.c.id.in_(ids), gates.c.team_id == team_id))} \
            if ids else {}
    for item in found["waiting"]:
        gate = held.get(item["gate_id"])
        role = rbac.role_for(granted.access, rbac.Target(team_id=team_id, project_id=gate.project_id)) \
            if gate is not None else None
        item["may_decide"] = gate is not None and gate.state == "open" and rbac.at_least(role, "project_lead") \
            and rbac.at_least(role, gate.min_role)
        item["expires_at"] = gate.expires_at if gate is not None else None
        if item["may_decide"]:
            item["nonce"] = gate.nonce
    return found


@router.get(BASE + "/fixes/{proposal_id}/patch")
def get_fix_patch(proposal_id: str, request: Request,
                  granted: Granted = Depends(require("viewer", by_team()))) -> PlainTextResponse:
    """A fix's stored patch, before anything reaches GitHub, as plain text a browser never renders."""
    srv, team_id = server(request), granted.target.team_id
    with srv.engine.connect() as conn:
        found = conn.execute(select(fix_proposals.c.patch).where(fix_proposals.c.id == proposal_id,
                                                                 fix_proposals.c.team_id == team_id)).first()
    if found is None or not found.patch:
        raise ApiError(404, "No such fix, or it has no change to show.")
    return PlainTextResponse(found.patch, headers={"X-Content-Type-Options": "nosniff",
                                                   "Content-Disposition": "inline"})


@router.get(f"{BASE}/findings/specialists/{{specialist}}")
def get_specialist(specialist: str, request: Request, days: str = Query("30", max_length=3),
                   granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    if len(specialist) > NAME_MAX:
        raise ApiError(400, "No such specialist.")
    srv, team_id, chosen, lead = server(request), granted.target.team_id, _days(days), _lead(granted)
    return _read(srv, team_id, "specialist", (chosen, specialist, lead), lambda conn: metrics.specialist_view(
        conn, team_id, srv.now(), chosen, specialist, with_people=lead))


@router.get(BASE + "/repos/{repo_id}/files")
def get_file(repo_id: str, request: Request, path: str = Query(..., min_length=1, max_length=PATH_MAX),
             days: str = Query("30", max_length=3),
             granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, team_id, chosen, lead = server(request), granted.target.team_id, _days(days), _lead(granted)
    with srv.engine.connect() as conn:
        _repo(conn, team_id, repo_id, 404)
    return _read(srv, team_id, "file", (chosen, repo_id, path, lead), lambda conn: metrics.file_view(
        conn, team_id, srv.now(), chosen, repo_id, path, with_people=lead))


@router.get(f"{BASE}/prs")
def get_prs(request: Request, days: str = Query("30", max_length=3), repo: str | None = Query(None, max_length=36),
            stage: str | None = Query(None, max_length=32), push_class: str | None = Query(None, max_length=32),
            category: str | None = Query(None, max_length=64), person: str | None = Query(None, max_length=36),
            granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    if stage and stage not in metrics.STAGES:
        raise ApiError(400, f"stage must be one of {', '.join(metrics.STAGES)}.")
    if category and category not in taxonomy.CATEGORIES:
        raise ApiError(400, "No such category.")
    if person and not _lead(granted):
        raise ApiError(403, "Not allowed.")
    srv, team_id, chosen, lead = server(request), granted.target.team_id, _days(days), _lead(granted)
    with srv.engine.connect() as conn:
        repo_id, person_id = _repo(conn, team_id, repo), _person(conn, team_id, person)
    params = (chosen, repo_id, stage or None, push_class or None, category or None, person_id, lead)
    return _read(srv, team_id, "prs", params, lambda conn: metrics.pr_list(
        conn, team_id, srv.now(), chosen, repo_id=repo_id, stage=stage or None, push_class=push_class or None,
        category=category or None, person_id=person_id, with_people=lead))


@router.get(BASE + "/repos/{repo_id}/prs/{number}")
def get_pr_by_number(repo_id: str, number: int, request: Request,
                     granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, team_id = server(request), granted.target.team_id
    with srv.engine.connect() as conn:
        _repo(conn, team_id, repo_id, 404)
        pr_id = metrics.pr_id_by_number(conn, team_id, repo_id, number)
    if pr_id is None:
        raise ApiError(404, "No such pull request.")
    return _pr_page(srv, team_id, pr_id)


def _pr_page(srv: Server, team_id: str, pr_id: str) -> dict[str, Any]:
    found = _read(srv, team_id, "pr", (pr_id,), lambda conn: metrics.pr_detail(conn, team_id, pr_id, srv.now()))
    if found is None:
        raise ApiError(404, "No such pull request.")
    return found


@router.get(BASE + "/prs/{pr_id}")
def get_pr(pr_id: str, request: Request, granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    return _pr_page(server(request), granted.target.team_id, pr_id)


@router.post(BASE + "/people/{person_id}/merge")
def merge_person(person_id: str, request: Request, payload: dict[str, Any] = Body(...),
                 granted: Granted = Depends(require(LEAD, by_team(), step_up=True))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        kept = people_admin.merge(conn, granted.target.team_id, person_id, payload.get("into"),
                                  actor_id=granted.user_id, ip=client_ip(request))
    answers.forget_team(granted.target.team_id)
    return {"person": people_admin.public(kept)}


@router.post(BASE + "/people/{person_id}/delete")
def delete_person(person_id: str, request: Request,
                  granted: Granted = Depends(require(LEAD, by_team(), step_up=True))) -> dict[str, Any]:
    with _refusals(), server(request).engine.begin() as conn:
        people_admin.remove(conn, granted.target.team_id, person_id, actor_id=granted.user_id,
                            ip=client_ip(request))
    answers.forget_team(granted.target.team_id)
    return {"ok": True}


# Comparisons and recommendations

@router.get(BASE + "/models/comparisons/{experiment_id}")
def get_comparison(experiment_id: str, request: Request,
                   granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    srv, team_id = server(request), granted.target.team_id

    def compute(conn: Connection) -> dict[str, Any]:
        return scoring.detail(conn, experiments.get(conn, team_id, experiment_id))
    with _refusals():
        return _read(srv, team_id, "comparison", (experiment_id,), compute)


@router.post(BASE + "/models/recommendations/{recommendation_id}/approve")
def approve_recommendation(recommendation_id: str, request: Request,
                           granted: Granted = Depends(require(LEAD, by_team(), step_up=True))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        done = recommendations.approve(conn, granted.target.team_id, recommendation_id, access=granted.access,
                                       actor_id=granted.user_id, now=srv.now(), ip=client_ip(request),
                                       catalog=defs.load_catalog(srv.cfg.packs_dir))
    answers.forget_team(granted.target.team_id)
    return done


@router.post(BASE + "/models/recommendations/{recommendation_id}/dismiss")
def dismiss_recommendation(recommendation_id: str, request: Request, payload: dict[str, Any] | None = Body(None),
                           granted: Granted = Depends(require(LEAD, by_team()))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        done = recommendations.dismiss(conn, granted.target.team_id, recommendation_id,
                                       (payload or {}).get("reason"), actor_id=granted.user_id, now=srv.now(),
                                       ip=client_ip(request))
    answers.forget_team(granted.target.team_id)
    return done


def _choices(conn: Connection, team_id: str, is_admin: bool = False) -> dict[str, Any]:
    """What the Add comparison form offers: each specialist a review-pass runs, with its current setting and the
    models its provider's aliases name, the efforts, and the defaults a new comparison starts with. An admin is
    also offered each alias the team can use on another provider."""
    current = {}
    aliases = llm.team_aliases(conn, team_id) if is_admin else []
    resolved = {alias: defs.resolve_alias(conn, alias, team_id, None) for alias in aliases}
    for specialist in risk.SPECIALISTS:
        try:
            setting, provider = experiments.current_setting(conn, team_id, None, specialist)
        except experiments.ExperimentError:
            continue
        named = sorted(set((provider.aliases or {}).values())) if provider is not None else []
        current[specialist] = {**setting, "models": named}
        if is_admin:
            current[specialist]["aliases"] = [
                {"alias": alias, "model": found[1], "provider": found[0].kind}
                for alias, found in resolved.items()
                if found is not None and (provider is None or found[0].id != provider.id)]
    return {"specialists": list(current), "efforts": list(experiments.EFFORTS), "current": current,
            "defaults": {name: default for name, (_, _, default) in experiments.LIMITS.items()},
            "lead_limits": experiments.LEAD_LIMITS}


@router.get(f"{BASE}/models/experiments")
def list_experiments(request: Request, granted: Granted = Depends(require(LEAD, by_team()))) -> dict[str, Any]:
    team_id = granted.target.team_id
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(eval_experiments).where(eval_experiments.c.team_id == team_id)
                            .order_by(eval_experiments.c.created_at.desc(), eval_experiments.c.id)).all()
        return {"experiments": [experiments.public(r) for r in rows],
                **_choices(conn, team_id, granted.access.is_admin)}


@router.post(f"{BASE}/models/experiments")
def create_experiment(request: Request, payload: dict[str, Any] = Body(...),
                      granted: Granted = Depends(require(LEAD, by_team()))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = experiments.create(conn, granted.target.team_id, payload, actor_id=granted.user_id, now=srv.now(),
                                 ip=client_ip(request), is_admin=granted.access.is_admin)
    answers.forget_team(granted.target.team_id)
    return {"experiment": experiments.public(row)}


@router.post(BASE + "/models/experiments/{experiment_id}/{action}")
def change_experiment(experiment_id: str, action: str, request: Request,
                      granted: Granted = Depends(require(LEAD, by_team()))) -> dict[str, Any]:
    srv = server(request)
    with _refusals(), srv.engine.begin() as conn:
        row = experiments.change_status(conn, granted.target.team_id, experiment_id, action,
                                        actor_id=granted.user_id, ip=client_ip(request))
    answers.forget_team(granted.target.team_id)
    return {"experiment": experiments.public(row)}
