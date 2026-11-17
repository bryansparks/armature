"""Experiments: what a team compares, the comparison runs queued when a sampled review ends, and the daemon that
queues, settles, judges, and scores them.

A review is sampled when it is queued, by its head and the experiment, so the choice never depends on how the review
went. Its two arms, the variant and the control, are queued only once it succeeded, so they can repeat its model and
its risk panel. Arms never cancel or wait for a review, and a review never cancels them.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, get_args

from sqlalchemy import Connection, Engine, and_, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.server import audit, defs, environments, risk, worker
from armature.server.crypto import Keyring
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    EVAL_ARM_LABELS,
    EVAL_VARIABLES,
    eval_arms,
    eval_experiments,
    jobs,
    llm_calls,
    projects,
    run_steps,
    runs,
    workflows,
)
from armature.server.insights import arm_matching, arms, judge, scoring
from armature.server.insights import jobs as insights_jobs
from armature.spec.models import ReasoningEffort

log = logging.getLogger(__name__)

TICK_S = 60.0
STOP_TIMEOUT_S = 5.0
# A review that ended longer ago than this is never compared, so a paused queue can't start a stale backlog.
WINDOW = timedelta(days=2)
REVIEW_PASS = "review-pass"
REVIEW_KIND = "review"
EFFORTS = get_args(ReasoningEffort)
ACTIVE = arms.ACTIVE
# The review-pass stages a comparison doesn't need, which answer with these outputs and start no session.
RATE, LEAD = "rate", "team_lead"
NO_LEAD = {"verdict": "comment", "summary": "A comparison run has no lead.", "decisions": [], "comments": []}
TERMINAL = ("done", "failed", "skipped")
ACTIONS = {"pause": (("running",), "paused"), "resume": (("paused",), "running"),
           "finish": (("running", "paused"), "finished")}
# A team lead's ceilings. Only an admin may sample more reviews or run more arms at once.
LEAD_LIMITS = {"sample_rate": 0.5, "max_concurrent_arms": 4}
LIMITS = {"sample_rate": (0.01, 1.0, 0.2), "daily_budget_usd": (1, 1000, 25), "max_concurrent_arms": (2, 20, 2),
          "min_prs": (5, 1000, 30)}
NO_ENVIRONMENT = f"No runner or environment type provides {worker.EVAL_ARM}."
JOB_ENDED = {"succeeded": "done", "failed": "failed", "canceled": "failed", "lost": "failed", "superseded": "failed"}


class ExperimentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def public(row: Any) -> dict[str, Any]:
    return {"id": row.id, "project_id": row.project_id, "specialist": row.specialist, "variable": row.variable,
            "baseline": row.baseline, "variant": row.variant, "sample_rate": row.sample_rate,
            "daily_budget_usd": float(row.daily_budget_usd), "max_concurrent_arms": row.max_concurrent_arms,
            "status": row.status, "min_prs": row.min_prs, "created_at": row.created_at.isoformat(),
            "created_by": row.created_by, "question": scoring.question(row)}


def get(conn: Connection, team_id: str, experiment_id: str) -> Any:
    row = conn.execute(select(eval_experiments).where(eval_experiments.c.team_id == team_id,
                                                      eval_experiments.c.id == experiment_id)).first()
    if row is None:
        raise ExperimentError(404, "No such comparison.")
    return row


def review_passes(conn: Connection, team_id: str, project_id: str | None) -> list[Any]:
    """The team's review-pass workflows the experiment compares, the project's only when it names one."""
    query = select(workflows).where(workflows.c.team_id == team_id, workflows.c.name == REVIEW_PASS)
    if project_id is not None:
        query = query.where(workflows.c.project_id == project_id)
    return list(conn.execute(query.order_by(workflows.c.scope, workflows.c.id)))


def agent_stage(body: str | None, specialist: str) -> dict[str, Any] | None:
    """The ``agent:`` mapping of the specialist's stage in a workflow body."""
    data = worker._load(body)
    stages = data.get("stages") if isinstance(data, dict) else None
    for stage in stages if isinstance(stages, list) else []:
        if isinstance(stage, dict) and stage.get("id") == specialist and isinstance(stage.get("agent"), dict):
            return stage["agent"]
    return None


def current_setting(conn: Connection, team_id: str, project_id: str | None,
                    specialist: str) -> tuple[dict[str, Any], Any]:
    """What the specialist runs now, its alias, the model that alias names, and its effort, and the provider row the
    alias resolves to."""
    for row in review_passes(conn, team_id, project_id):
        agent = agent_stage(defs.body_of(conn, defs.WORKFLOW, row.id, row.current_version), specialist)
        if agent is None or not isinstance(agent.get("model"), str):
            continue
        found = defs.resolve_alias(conn, agent["model"], team_id, row.project_id)
        return ({"alias": agent["model"], "model": found[1] if found else None, "effort": agent.get("effort")},
                found[0] if found else None)
    raise ExperimentError(409, f"No review-pass workflow in this team has a {specialist} step to compare.")


def resolve_variant(conn: Connection, team_id: str, project_id: str | None, specialist: str, alias: str) -> Any:
    """The provider row and model ``alias`` names for every review-pass with a specialist step, resolved in each
    one's project as the baseline is. Refused unless they all agree."""
    found = {}
    for row in review_passes(conn, team_id, project_id):
        agent = agent_stage(defs.body_of(conn, defs.WORKFLOW, row.id, row.current_version), specialist)
        if agent is None or not isinstance(agent.get("model"), str):
            continue
        hit = defs.resolve_alias(conn, alias, team_id, row.project_id)
        if hit is None:
            raise ExperimentError(400, f"No provider defines the alias {alias} for every review-pass workflow.")
        found[(hit[0].id, hit[1])] = hit
    if len(found) != 1:
        raise ExperimentError(400, f"The alias {alias} names another provider or model in another project's "
                                   "review-pass workflow.")
    return next(iter(found.values()))


def baseline(conn: Connection, team_id: str, project_id: str | None, specialist: str) -> dict[str, Any]:
    return current_setting(conn, team_id, project_id, specialist)[0]


def _number(body: dict[str, Any], name: str, integer: bool) -> Any:
    low, high, default = LIMITS[name]
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)) \
            or not low <= value <= high:
        raise ExperimentError(400, f"{name} must be a {'whole ' if integer else ''}number from {low} to {high}.")
    return value


def create(conn: Connection, team_id: str, body: Any, *, actor_id: str, now: datetime, ip: str | None,
           is_admin: bool = False) -> Any:
    """Start an experiment. A team lead may compare only a model an alias on the specialist's provider already
    names, within LEAD_LIMITS. An admin may compare any model id, or an alias on another provider, at any limit."""
    if not isinstance(body, dict):
        raise ExperimentError(400, "The body must be an object.")
    specialist, variable, value = body.get("specialist"), body.get("variable"), body.get("variant")
    alias = body.get("alias") if variable == "model" else None
    if alias is not None and not (isinstance(alias, str) and defs.ALIAS.fullmatch(alias)):
        raise ExperimentError(400, "An alias must be a model alias, such as review.")
    if specialist not in risk.SPECIALISTS:
        raise ExperimentError(400, f"specialist must be one of {', '.join(risk.SPECIALISTS)}.")
    if variable not in EVAL_VARIABLES:
        raise ExperimentError(400, "variable must be model or effort.")
    if variable == "effort" and value not in EFFORTS:
        raise ExperimentError(400, f"An effort must be one of {', '.join(EFFORTS)}.")
    if variable == "model" and alias is None and not (isinstance(value, str) and defs.MODEL_ID.fullmatch(value)):
        raise ExperimentError(400, "A model must be a model id, such as claude-sonnet-4-6.")
    project_id = body.get("project_id") or None
    if project_id is not None and conn.execute(select(projects.c.id).where(
            projects.c.id == project_id, projects.c.team_id == team_id)).first() is None:
        raise ExperimentError(404, "No such project in this team.")
    numbers = {name: _number(body, name, name in ("max_concurrent_arms", "min_prs")) for name in LIMITS}
    over = [name for name, most in LEAD_LIMITS.items() if numbers[name] > most]
    if over and not is_admin:
        raise ExperimentError(403, f"Only an admin can set {' or '.join(over)} above "
                                   f"{' and '.join(f'{LEAD_LIMITS[n]:g}' for n in over)}.")
    current, provider = current_setting(conn, team_id, project_id, specialist)
    variant: dict[str, Any] = {variable: value}
    crosses = False
    if alias is not None:
        found = resolve_variant(conn, team_id, project_id, specialist, alias)
        config = found[0].config or {}
        if config.get("api_base") and not (config.get("prices") or {}).get(alias):
            raise ExperimentError(400, f"Set a price for {alias} on its provider first, so its cost can be compared.")
        value, variant = found[1], {"model": found[1], "alias": alias}
        crosses = provider is None or found[0].id != provider.id
        # The variant arm runs on that provider, with its credential, so only an admin may send reviews there.
        if crosses and not is_admin:
            raise ExperimentError(403, "That alias is on another provider. Only an admin can compare models across "
                                       "providers.")
    named = set((provider.aliases or {}).values()) if provider is not None else set()
    if variable == "model" and not is_admin and value not in named:
        raise ExperimentError(403, "Pick a model one of this provider's aliases already names. Only an admin can "
                                   "compare any model.")
    if alias == current.get("alias") or (current.get(variable) == value and not crosses):
        raise ExperimentError(400, f"The {specialist} step already runs that {variable}.")
    open_one = conn.execute(select(eval_experiments.c.id).where(
        eval_experiments.c.team_id == team_id, eval_experiments.c.specialist == specialist,
        eval_experiments.c.variable == variable, eval_experiments.c.status != "finished",
        eval_experiments.c.project_id.is_(None) if project_id is None
        else eval_experiments.c.project_id == project_id)).first()
    if open_one is not None:
        raise ExperimentError(409, f"A comparison of {specialist}'s {variable} is already open. Finish it first.")
    experiment_id = new_id()
    conn.execute(insert(eval_experiments).values(
        id=experiment_id, team_id=team_id, project_id=project_id, specialist=specialist, variable=variable,
        baseline=current, variant=variant, status="running", created_at=now, created_by=actor_id,
        **numbers))
    audit.record(conn, action="insights.experiment.create", actor_id=actor_id, team_id=team_id,
                 target_kind="experiment", target_id=experiment_id,
                 detail={"specialist": specialist, "variable": variable, "variant": value, "alias": alias,
                         "baseline": current,
                         "project_id": project_id, **numbers}, ip=ip)
    return get(conn, team_id, experiment_id)


def change_status(conn: Connection, team_id: str, experiment_id: str, action: str, *, actor_id: str,
                  ip: str | None) -> Any:
    if action not in ACTIONS:
        raise ExperimentError(404, "No such action.")
    row = get(conn, team_id, experiment_id)
    allowed, status = ACTIONS[action]
    if row.status not in allowed:
        raise ExperimentError(409, f"A {row.status} comparison can't {action}.")
    conn.execute(update(eval_experiments).where(eval_experiments.c.id == row.id).values(status=status))
    audit.record(conn, action=f"insights.experiment.{action}", actor_id=actor_id, team_id=team_id,
                 target_kind="experiment", target_id=row.id, detail={"from": row.status, "to": status}, ip=ip)
    return get(conn, team_id, experiment_id)


# Queueing

def _primary_call(conn: Connection, run_id: str, step: str) -> Any:
    """The review's specialist step's last uncached call with a model: the model id it ran, and the alias and
    effort it asked for."""
    return conn.execute(select(llm_calls.c.model_id, llm_calls.c.alias, llm_calls.c.requested_effort).where(
        llm_calls.c.run_id == run_id, llm_calls.c.step_key == step, llm_calls.c.cached.is_(False),
        llm_calls.c.model_id.is_not(None)).order_by(llm_calls.c.seq.desc()).limit(1)).first()


def overrides(experiment: Any, label: str, model_id: str, risk: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The stage settings an arm runs: the control repeats the primary's model by id, and the variant changes one
    thing. The rate and lead stages answer from the primary's risk without a session, since only the
    specialist's findings are compared."""
    pinned: dict[str, Any] = {"model": model_id}
    if label == "variant":
        pinned.update(experiment.variant)
    return {experiment.specialist: pinned, LEAD: {"output": NO_LEAD},
            RATE: {"output": {"level": risk["level"], "reasons": [], "commit": risk["head"]}}}


def _can_run(conn: Connection, team_id: str, requires: list[str], now: datetime) -> bool:
    """Whether a runner online now or an environment type would take a job with these requirements."""
    job = SimpleNamespace(team_id=team_id, requires=requires)
    if environments.taken(job, environments.takers(conn, now)):
        return True
    aws = environments.connection(conn)
    return aws is not None and aws.state == "ok" and environments.arm_type(conn, requires) is not None


def _primaries(conn: Connection, experiment: Any, now: datetime) -> list[Any]:
    """The experiment's sampled reviews that succeeded since it began and have no arms yet, oldest first."""
    reviewed = exists().where(run_steps.c.run_id == runs.c.id, run_steps.c.step_id == arms.RISK_STEP,
                              run_steps.c.status == "succeeded")
    armed = exists().where(eval_arms.c.experiment_id == experiment.id, eval_arms.c.primary_run_id == runs.c.id)
    query = (select(runs.c.id, runs.c.project_id, jobs.c.workflow_id, jobs.c.workflow_version, jobs.c.inputs,
                    jobs.c.concurrency_key, jobs.c.call_pins, jobs.c.requires)
             .select_from(runs.join(jobs, and_(jobs.c.run_id == runs.c.id, jobs.c.resume_of_gate.is_(None)))
                          .join(workflows, workflows.c.id == jobs.c.workflow_id))
             .where(runs.c.team_id == experiment.team_id, runs.c.status == "succeeded", jobs.c.status == "succeeded",
                    jobs.c.arm_id.is_(None), jobs.c.dry_run.is_(False), workflows.c.kind == REVIEW_KIND,
                    jobs.c.created_at >= experiment.created_at,
                    runs.c.finished_at >= max(now - WINDOW, experiment.created_at), reviewed, ~armed))
    if experiment.project_id is not None:
        query = query.where(runs.c.project_id == experiment.project_id)
    rows = conn.execute(query.order_by(runs.c.finished_at, runs.c.id)).all()
    # A head is compared once, however many reviews it had.
    armed_heads = set(conn.execute(select(eval_arms.c.head_sha).where(
        eval_arms.c.experiment_id == experiment.id)).scalars())
    return [r for r in rows if isinstance(r.inputs, dict) and isinstance(r.inputs.get("ref"), str)
            and r.inputs["ref"] not in armed_heads
            and arms.sampled(r.inputs["ref"], experiment.id, experiment.sample_rate)]


def _skip_reason(conn: Connection, experiment: Any, primary: Any, now: datetime) -> tuple[str | None, Any]:
    """Why the primary gets no arms, or None with what they pin: the review's risk answer and specialist model."""
    found = arms.primary_risk(conn, primary.id)
    if found is None:
        return arms.NO_RISK, None
    if experiment.specialist not in found["panel"]:
        return f"{experiment.specialist} wasn't on this review's panel.", None
    step = arms.review_step(conn, primary.id, experiment.specialist)
    call = _primary_call(conn, primary.id, step) if step else None
    if call is None:
        return f"The review recorded no model for {experiment.specialist}.", None
    # Only reviews that ran the baseline's setting are pooled, so every comparison measures the same change.
    if (call.alias, call.requested_effort) != (experiment.baseline.get("alias"), experiment.baseline.get("effort")):
        return f"This review ran {experiment.specialist} with another setting than the comparison's baseline.", None
    budget = Decimal(str(experiment.daily_budget_usd))
    if arms.spent_today(conn, experiment.id, now) >= budget:
        return f"Today's comparison budget of ${float(budget):g} is spent.", None
    if arms.committed(conn, experiment, now, len(EVAL_ARM_LABELS)) > budget:
        return (f"Today's comparison budget of ${float(budget):g} doesn't cover two more comparison runs on top of "
                "those already running."), None
    if not _can_run(conn, experiment.team_id, sorted({*(primary.requires or ()), worker.EVAL_ARM}), now):
        return NO_ENVIRONMENT, None
    return None, (found, call.model_id)


def _arm_rows(experiment: Any, primary: Any, head: str, status: str, reason: str | None, pins: Any,
              now: datetime) -> list[dict[str, Any]]:
    return [{"id": new_id(), "team_id": experiment.team_id, "experiment_id": experiment.id,
             "primary_run_id": primary.id, "head_sha": head, "label": label, "status": status, "reason": reason,
             "overrides": overrides(experiment, label, pins[1], pins[0]) if pins else None, "created_at": now}
            for label in EVAL_ARM_LABELS]


def _queue(conn: Connection, experiment: Any, primary: Any, row: dict[str, Any], now: datetime) -> str:
    inputs = primary.inputs
    key = f"{(primary.concurrency_key or primary.id)[:140]}#arm:{row['id']}"
    job_id = worker.queue_job(conn, team_id=experiment.team_id, project_id=primary.project_id,
                              workflow_id=primary.workflow_id, workflow_version=primary.workflow_version,
                              inputs={"repo": inputs.get("repo"), "slug": inputs.get("slug"),
                                      "number": inputs.get("number"), "ref": row["head_sha"], "branch": "",
                                      "auto_fix": False, "round": 1},
                              now=now, key=key, arm_id=row["id"])
    # The arm repeats the primary's callees too, at the versions the primary ran.
    conn.execute(update(jobs).where(jobs.c.id == job_id).values(
        call_pins=primary.call_pins, requires=sorted({*(primary.requires or ()), worker.EVAL_ARM})))
    return job_id


def queue_arms(engine: Engine, experiment: Any, now: datetime) -> dict[str, int]:
    """Queue the arms of the experiment's sampled reviews that ended, or skip them saying why, up to its cap."""
    counts = {"queued": 0, "skipped": 0}
    with engine.connect() as conn:
        primaries = _primaries(conn, experiment, now)
    heads: set[str] = set()
    for primary in primaries:
        if primary.inputs["ref"] in heads:
            continue
        heads.add(primary.inputs["ref"])
        with engine.begin() as conn:
            active = conn.execute(select(func.count()).select_from(eval_arms).where(
                eval_arms.c.experiment_id == experiment.id, eval_arms.c.status.in_(ACTIVE))).scalar_one()
            reason, pins = _skip_reason(conn, experiment, primary, now)
            if reason is None and active + len(EVAL_ARM_LABELS) > experiment.max_concurrent_arms:
                break
            head = pins[0]["head"] if pins else (primary.inputs.get("ref") or "")[:40]
            rows = _arm_rows(experiment, primary, head, "skipped" if reason else "queued", reason, pins, now)
            try:
                with conn.begin_nested():
                    conn.execute(insert(eval_arms), rows)
            except IntegrityError:
                # Another server armed this review first.
                continue
            if reason is None:
                for row in rows:
                    _queue(conn, experiment, primary, row, now)
        counts["skipped" if reason else "queued"] += len(rows)
    return counts


def settle(engine: Engine, now: datetime) -> set[str]:
    """Move each active arm to its job's state. Returns the experiments with an arm that ended."""
    with engine.begin() as conn:
        rows = conn.execute(select(eval_arms.c.id, eval_arms.c.experiment_id, eval_arms.c.primary_run_id,
                                   eval_arms.c.status, jobs.c.status.label("job_status"), jobs.c.error,
                                   jobs.c.run_id, runs.c.cost_usd, runs.c.started_at, runs.c.finished_at)
                            .select_from(eval_arms.join(jobs, jobs.c.arm_id == eval_arms.c.id)
                                         .outerjoin(runs, runs.c.id == jobs.c.run_id))
                            .where(eval_arms.c.status.in_(ACTIVE))).all()
        ended = set()
        for row in rows:
            status = JOB_ENDED.get(row.job_status)
            if status is None:
                if row.status == "queued" and row.run_id is not None:
                    conn.execute(update(eval_arms).where(eval_arms.c.id == row.id).values(status="running"))
                continue
            if status == "failed" and row.run_id is None:
                status = "skipped"
            took = (row.finished_at - row.started_at).total_seconds() \
                if row.finished_at and row.started_at else None
            conn.execute(update(eval_arms).where(eval_arms.c.id == row.id, eval_arms.c.status.in_(ACTIVE)).values(
                status=status, reason=None if status == "done" else (row.error or "The comparison run ended.")[:2000],
                cost_usd=row.cost_usd, latency_s=took))
            ended.add(row.experiment_id)
    return ended


def evaluate(engine: Engine, keyring: Keyring, experiment: Any, now: datetime, changed: bool = False) -> None:
    """Match the reviews whose arms all ended, judge what is unjudged within the budget, let later evidence correct
    the judge, and score again when any of it, or ``changed`` arms, moved."""
    with engine.begin() as conn:
        matched = sum(arm_matching.match(conn, experiment, primary_run_id)
                      for primary_run_id in arm_matching.pending(conn, experiment))
    judged = judge.judge_experiment(engine, keyring, experiment, now) if experiment.status != "finished" else 0
    with engine.begin() as conn:
        confirmed = judge.apply_evidence(conn, experiment)
        if changed or matched or judged or confirmed:
            scoring.store(conn, experiment, scoring.compute(conn, experiment), now)


class Arms:
    """Every ``interval`` seconds: queue the arms of sampled reviews that ended, settle arms that ended, then
    match, judge, and score them."""

    def __init__(self, engine: Engine, keyring: Keyring, *, clock: Callable[[], datetime] = utcnow,
                 interval: float = TICK_S):
        self.engine = engine
        self.keyring = keyring
        self.interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="armature-insights-arms", daemon=True)
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
                log.exception("comparison runs tick failed")
            self._stop.wait(self.interval)

    def tick(self) -> None:
        now = self._clock()
        changed = settle(self.engine, now)
        with self.engine.connect() as conn:
            listed = conn.execute(select(eval_experiments).order_by(eval_experiments.c.created_at,
                                                                    eval_experiments.c.id)).all()
        for experiment in listed:
            if experiment.status == "finished" and experiment.id not in changed:
                continue
            # One server works an experiment each tick, so two never queue or judge the same review.
            slot = f"{insights_jobs.ARM_SLOT_PREFIX}{experiment.id}"
            if not insights_jobs.claim(self.engine, experiment.team_id, slot, now,
                                       now + timedelta(seconds=self.interval)):
                continue
            try:
                if experiment.status == "running":
                    queue_arms(self.engine, experiment, now)
                evaluate(self.engine, self.keyring, experiment, now, experiment.id in changed)
            except Exception as exc:
                log.error("comparison %s failed this tick: %s", experiment.id, type(exc).__name__)
