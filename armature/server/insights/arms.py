"""Comparison runs, called arms in code: which runs are arms, what a primary review pins for its arms, and which
reviews an experiment samples.

An arm reviews a sampled pull request again at the primary review's head, with one specialist's model or effort
changed, or, as the control, unchanged. Arm runs and their findings stay out of every insights measure.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Select, func, select

from armature.server.db.schema import (
    INSTANCE_SCOPE,
    RISK_LEVELS,
    eval_arms,
    eval_experiments,
    eval_judgments,
    llm_calls,
    run_steps,
    settings,
)
from armature.server.ingest import TRUNCATED

NO_RISK = "The review this comparison run repeats has no recorded risk and panel."
MOVED = "The pull request moved past the head this comparison run reviews, so the comparison is dropped."
ESTIMATE_SETTING = "insights.arm_cost_estimate_usd"
DEFAULT_ESTIMATE = Decimal("1.50")
ACTIVE = ("queued", "running")
RISK_STEP = "risk"


def arm_runs(team_id: str | None = None) -> Select:
    """The ids of every arm's run, the team's only when ``team_id`` is given."""
    query = select(eval_arms.c.arm_run_id).where(eval_arms.c.arm_run_id.is_not(None))
    return query.where(eval_arms.c.team_id == team_id) if team_id else query


def not_arm(run_id: Any, team_id: str | None = None) -> Any:
    """A condition true of a run id column's rows whose run is no arm's."""
    return run_id.not_in(arm_runs(team_id))


def sampled(head_sha: str, experiment_id: str, rate: float) -> bool:
    """Whether the experiment samples the review of ``head_sha``. The same pair always answers the same way."""
    digest = hashlib.sha256(f"{head_sha}:{experiment_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64 < rate


def primary_risk(conn: Connection, run_id: str) -> dict[str, Any] | None:
    """The first review pass's risk answer in the run: its level, panel, and head, or None when it has none whole."""
    found = conn.execute(select(run_steps.c.output).where(
        run_steps.c.run_id == run_id, run_steps.c.step_id == RISK_STEP, run_steps.c.status == "succeeded")
        .order_by(run_steps.c.step_key).limit(1)).scalar()
    whole = isinstance(found, dict) and not found.get(TRUNCATED) and found.get("level") in RISK_LEVELS \
        and isinstance(found.get("panel"), list) and isinstance(found.get("head"), str)
    return found if whole else None


def review_step(conn: Connection, run_id: str, specialist: str) -> str | None:
    """The step key of the specialist in the run's first review pass, beside its risk step, such as
    ``first/concurrency``."""
    risk_key = conn.execute(select(run_steps.c.step_key).where(
        run_steps.c.run_id == run_id, run_steps.c.step_id == RISK_STEP).order_by(run_steps.c.step_key)
        .limit(1)).scalar()
    if risk_key is None:
        return None
    parent = risk_key.rpartition("/")[0]
    return f"{parent}/{specialist}" if parent else specialist


def pinned(conn: Connection, arm_id: str) -> dict[str, Any] | None:
    """The head an arm reviews and the primary's risk answer at that head, its panel cut to the experiment's
    specialist, so the arm runs only the session it compares. None without the arm, and a None risk when the
    primary recorded none."""
    arm = conn.execute(select(eval_arms.c.primary_run_id, eval_arms.c.head_sha, eval_experiments.c.specialist)
                       .join(eval_experiments, eval_experiments.c.id == eval_arms.c.experiment_id)
                       .where(eval_arms.c.id == arm_id)).first()
    if arm is None:
        return None
    risk = primary_risk(conn, arm.primary_run_id)
    if risk is None:
        return {"head": arm.head_sha, "risk": None}
    panel = [arm.specialist] if arm.specialist in risk["panel"] else []
    return {"head": arm.head_sha, "risk": {**risk, "head": arm.head_sha, "panel": panel,
                                           "skipped": [s for s in risk["panel"] if s not in panel]}}


def spent_today(conn: Connection, experiment_id: str, now: datetime) -> Decimal:
    """What the experiment spent since UTC midnight: its arms' model calls, and its judge."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    runs_of = select(eval_arms.c.arm_run_id).where(eval_arms.c.experiment_id == experiment_id,
                                                   eval_arms.c.arm_run_id.is_not(None))
    calls = conn.execute(select(func.coalesce(func.sum(llm_calls.c.cost_usd), 0)).where(
        llm_calls.c.run_id.in_(runs_of), llm_calls.c.cached.is_(False),
        llm_calls.c.created_at >= midnight)).scalar_one()
    judged = conn.execute(select(func.coalesce(func.sum(eval_judgments.c.cost_usd), 0)).where(
        eval_judgments.c.experiment_id == experiment_id, eval_judgments.c.created_at >= midnight)).scalar_one()
    return Decimal(str(calls)) + Decimal(str(judged))


def expected_cost(conn: Connection, experiment: Any) -> Decimal:
    """What one arm of the experiment is expected to cost: the mean of its finished arms, else the team's
    insights.arm_cost_estimate_usd setting, else DEFAULT_ESTIMATE."""
    mean = conn.execute(select(func.avg(eval_arms.c.cost_usd)).where(
        eval_arms.c.experiment_id == experiment.id, eval_arms.c.status == "done",
        eval_arms.c.cost_usd.is_not(None))).scalar()
    if mean is not None:
        return Decimal(str(mean))
    found = dict(conn.execute(select(settings.c.scope, settings.c.value).where(
        settings.c.key == ESTIMATE_SETTING, settings.c.scope.in_([experiment.team_id, INSTANCE_SCOPE]))).all())
    value = found.get(experiment.team_id, found.get(INSTANCE_SCOPE))
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
    return Decimal(str(value)) if ok else DEFAULT_ESTIMATE


def committed(conn: Connection, experiment: Any, now: datetime, new: int = 0) -> Decimal:
    """Today's recorded spend plus every queued or running arm, and ``new`` arms about to queue, at the expected
    cost, so the arms that start can't carry the day past its budget."""
    active = conn.execute(select(func.count()).select_from(eval_arms).where(
        eval_arms.c.experiment_id == experiment.id, eval_arms.c.status.in_(ACTIVE))).scalar_one()
    return spent_today(conn, experiment.id, now) + (active + new) * expected_cost(conn, experiment)


def over_budget(conn: Connection, experiment: Any, now: datetime) -> bool:
    """Whether the experiment's spend today reached its daily budget, so no arm starts and the judge waits."""
    return spent_today(conn, experiment.id, now) >= Decimal(str(experiment.daily_budget_usd))
