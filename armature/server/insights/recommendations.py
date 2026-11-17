"""A team lead's decision on a recommendation. Approving applies its change as a new version of each review-pass
workflow the experiment compared: the specialist's ``effort:``, or its ``model:`` alias, adding the alias
``review-<specialist>`` to the provider the step's alias resolves from when no alias there names the model yet.

A variant alias on another provider becomes the step's alias as it is, and nothing is added to the baseline's
provider.

Only an admin may change an ``agent:`` field through the editor. An effort change is narrow, so a team lead who may
write every review-pass row approves it, and the server saves the one field it builds from the stored
recommendation with its own admin access. A model change needs an admin.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select, update

from armature.server import audit, defs, rbac
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    eval_experiments,
    eval_recommendations,
)
from armature.server.insights import experiments, scoring

REASON_MAX = 500
SERVER = rbac.Access(is_admin=True)
MODEL_NEEDS_ADMIN = "A model change needs an admin to approve."


class RecommendationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _waiting(conn: Connection, team_id: str, recommendation_id: str) -> Any:
    row = conn.execute(select(eval_recommendations).where(eval_recommendations.c.team_id == team_id,
                                                          eval_recommendations.c.id == recommendation_id)).first()
    if row is None:
        raise RecommendationError(404, "No such recommendation.")
    if row.state != "waiting":
        raise RecommendationError(409, f"This recommendation was already {row.state}.")
    return row


def _decide(conn: Connection, recommendation_id: str, **values: Any) -> None:
    """Move a waiting recommendation on, refusing when another request decided it first."""
    if conn.execute(update(eval_recommendations).where(eval_recommendations.c.id == recommendation_id,
                                                       eval_recommendations.c.state == "waiting")
                    .values(**values)).rowcount != 1:
        raise RecommendationError(409, "Someone else decided this recommendation first.")


def _alias_for(conn: Connection, access: rbac.Access, team_id: str, workflow: Any, alias: str, model: str,
               specialist: str, actor_id: str, now: datetime, ip: str | None, variant_alias: str | None = None) -> str:
    """An alias for ``model`` on the provider ``alias`` resolves from for the workflow: one already there, else
    ``review-<specialist>``, which this adds. A variant alias on another provider is the alias itself."""
    found = defs.resolve_alias(conn, alias, team_id, workflow.project_id)
    if found is None:
        raise RecommendationError(409, f"No provider defines the alias {alias} any longer.")
    provider = found[0]
    if variant_alias is not None:
        variant = defs.resolve_alias(conn, variant_alias, team_id, workflow.project_id)
        if variant is None or variant[1] != model:
            raise RecommendationError(409, f"The alias {variant_alias} no longer names {model}, so this "
                                           "recommendation no longer applies. Dismiss it.")
        if variant[0].id != provider.id:
            return variant_alias
    aliases = dict(provider.aliases or {})
    named = f"review-{specialist}"
    existing = sorted(a for a, m in aliases.items() if m == model)
    if existing:
        return named if named in existing else existing[0]
    if named in aliases:
        raise RecommendationError(409, f"The alias {named} already names {aliases[named]}. Change it on the "
                                       "provider first.")
    if provider.scope == INSTANCE_SCOPE and not access.is_admin:
        raise RecommendationError(403, f"The {alias} alias comes from a provider for all teams, so only an admin can "
                                       "add an alias to it.")
    need = "project_lead" if provider.project_id else "team_lead"
    if not access.is_admin and not rbac.at_least(rbac.role_for(access, defs.provider_target(conn, provider.id)), need):
        raise RecommendationError(403, "Adding the alias needs a lead of the provider's team or project.")
    defs.update_provider(conn, provider_id=provider.id, body={"aliases": {**aliases, named: model}},
                         actor_id=actor_id, now=now, ip=ip, is_admin=access.is_admin, may_name_secret=False)
    return named


def approve(conn: Connection, team_id: str, recommendation_id: str, *, access: rbac.Access, actor_id: str,
            now: datetime, ip: str | None, catalog: Any = None) -> dict[str, Any]:
    """Apply the change to every review-pass the experiment compared, as new versions, then finish the experiment.
    Returns the recommendation and what each workflow changed from and to."""
    row = _waiting(conn, team_id, recommendation_id)
    experiment = conn.execute(select(eval_experiments).where(eval_experiments.c.id == row.experiment_id)).one()
    targets = experiments.review_passes(conn, team_id, experiment.project_id)
    if not targets:
        raise RecommendationError(409, "This team no longer has a review-pass workflow to change.")
    effort = "effort" in row.change
    if effort and row.change["effort"] not in experiments.EFFORTS:
        raise RecommendationError(409, "This recommendation names an effort that doesn't exist.")
    if not effort and not access.is_admin:
        raise RecommendationError(403, MODEL_NEEDS_ADMIN)
    if any(not defs.can_write(access, defs.row_scope(workflow)) for workflow in targets):
        raise RecommendationError(403, "Approving needs write access to every project's review-pass workflow.")
    specialist, applied = row.specialist, []
    expected = {"model": experiment.baseline.get("alias"), "effort": experiment.baseline.get("effort")}
    current = []
    for workflow in targets:
        body = defs.body_of(conn, defs.WORKFLOW, workflow.id, workflow.current_version)
        agent = experiments.agent_stage(body, specialist)
        if agent is None or not isinstance(agent.get("model"), str):
            raise RecommendationError(409, f"A review-pass workflow has no {specialist} step any longer.")
        before = {"model": agent["model"], "effort": agent.get("effort")}
        # The evidence is about the baseline, so a step someone changed since gets nothing applied.
        if before != expected:
            raise RecommendationError(409, f"The {specialist} step of a review-pass changed since the comparison "
                                           "began, so this recommendation no longer applies. Dismiss it.")
        current.append((workflow, body, agent, before))
    for workflow, body, agent, before in current:
        if effort:
            field, value = "effort", row.change["effort"]
        else:
            field, value = "model", _alias_for(conn, access, team_id, workflow, agent["model"], row.change["model"],
                                               specialist, actor_id, now, ip, experiment.variant.get("alias"))
        try:
            saved, _ = defs.save(conn, defs.WORKFLOW, workflow, access=SERVER if effort else access,
                                 body=defs.set_agent_field(body, specialist, field, value),
                                 note=f"Apply the comparison's recommendation for {specialist}",
                                 base_version=workflow.current_version, actor_id=actor_id, now=now, ip=ip,
                                 catalog=catalog)
        except defs.DefError as exc:
            raise RecommendationError(exc.status, exc.message) from exc
        applied.append({"workflow_id": workflow.id, "project_id": workflow.project_id,
                        "version": saved.current_version, "before": before, "after": {**before, field: value}})
    versions = [{"workflow_id": a["workflow_id"], "version": a["version"]} for a in applied]
    _decide(conn, row.id, state="applied", decided_by=actor_id, decided_at=now, applied_version=versions)
    conn.execute(update(eval_experiments).where(eval_experiments.c.id == experiment.id).values(status="finished"))
    audit.record(conn, action="insights.recommendation.approve", actor_id=actor_id, team_id=team_id,
                 target_kind="recommendation", target_id=row.id,
                 detail={"experiment_id": experiment.id, "specialist": specialist, "change": row.change,
                         "applied": applied}, ip=ip)
    found = conn.execute(select(eval_recommendations).where(eval_recommendations.c.id == row.id)).one()
    return {"recommendation": scoring.recommendation(found), "applied": applied}


def dismiss(conn: Connection, team_id: str, recommendation_id: str, reason: Any, *, actor_id: str, now: datetime,
            ip: str | None) -> dict[str, Any]:
    if reason is not None and not isinstance(reason, str):
        raise RecommendationError(400, "reason must be text.")
    row = _waiting(conn, team_id, recommendation_id)
    _decide(conn, row.id, state="dismissed", decided_by=actor_id, decided_at=now)
    audit.record(conn, action="insights.recommendation.dismiss", actor_id=actor_id, team_id=team_id,
                 target_kind="recommendation", target_id=row.id,
                 detail={"experiment_id": row.experiment_id, "change": row.change,
                         "reason": (reason or "").strip()[:REASON_MAX] or None}, ip=ip)
    found = conn.execute(select(eval_recommendations).where(eval_recommendations.c.id == row.id)).one()
    return {"recommendation": scoring.recommendation(found)}
