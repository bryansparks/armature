"""Score an experiment's comparison runs, and turn a result that counts into a recommendation a team lead can approve.

The findings of one pull request form issues: each primary finding is one, a comparison run's finding joins the
primary finding it matched, else the other run's finding it matched, else it is an issue of its own. An issue is real
when the judge, or later evidence, found any of its findings real. Per run, recall is the share of the pull request's
real issues it raised, precision the share of its findings that were real, and unique the real issues only it raised.
Cost and latency are the specialist step's.

The variant and the control run under the same conditions, after the primary review, so the effect is variant minus
control, measured per pull request. It counts only when the mean difference is more than twice its standard error
and at least min_prs pull requests were compared. Control minus primary is the environment shift: what running later,
as a comparison, changes on its own. It is shown and never decides.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime
from statistics import mean, stdev
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select

from armature.server.db import new_id
from armature.server.db.schema import (
    EVAL_SCORE_LABELS,
    eval_arms,
    eval_experiments,
    eval_judgments,
    eval_matches,
    eval_recommendations,
    eval_scores,
    jobs,
    llm_calls,
    run_steps,
)
from armature.server.insights import arm_matching, arms

SCORER_VERSION = "2"
MEASURES = ("recall", "precision", "unique_valid", "cost_usd")
PRIMARY, VARIANT, CONTROL = "primary", "variant", "control"
# A difference counts when its mean is more than this many standard errors from zero.
SE_MULTIPLE = 2
EARLY = "Too early to tell"
WITHIN = "Within normal variation"
RECOMMENDS = ("cheaper", "better")
# Proposals a team lead already decided are never made again for the same experiment.
DECIDED = ("waiting", "dismissed")
MODEL_WORDS = {"gpt": "GPT", "ai": "AI", "xl": "XL"}


def specialist_name(name: str) -> str:
    return name.replace("_", " ")


def model_name(model_id: Any) -> str:
    """A model id as its product name, as the Models screen's modelName writes it: "claude-opus-4-6" reads
    "Claude Opus 4.6". A date suffix is dropped."""
    parts = [p for p in re.split(r"[-_\s]+", str(model_id or "")) if p]
    if len(parts) > 1 and re.fullmatch(r"\d{8}", parts[-1]):
        parts.pop()
    words: list[str] = []
    for part in parts:
        if part.isdigit() and words and re.fullmatch(r"\d+(\.\d+)*", words[-1]):
            words[-1] = f"{words[-1]}.{part}"
        else:
            words.append(MODEL_WORDS.get(part.lower()) or (part if part[0].isdigit() else part[0].upper() + part[1:]))
    return " ".join(words) or "Unknown model"


def _effort(value: Any) -> str:
    return value if isinstance(value, str) and value else "the default effort"


def sides(experiment: Any) -> tuple[str, str]:
    """How a sentence names the variant and the setting it is compared with."""
    if experiment.variable == "effort":
        return f"at {experiment.variant['effort']} effort", _effort(experiment.baseline.get("effort"))
    variant, current = experiment.variant, experiment.baseline
    if variant.get("alias") and variant["model"] == current.get("model"):
        # The same model on two providers, told apart by alias.
        name = model_name(variant["model"])
        return f"on {name} ({variant['alias']})", f"{name} ({current.get('alias')})"
    return f"on {model_name(variant['model'])}", model_name(current.get("model") or current.get("alias"))


def question(experiment: Any) -> str:
    variant, current = sides(experiment)
    current = current if experiment.variable == "model" or current.startswith("the ") else f"{current} effort"
    return f"{specialist_name(experiment.specialist).capitalize()} {variant} instead of {current}"


def _pairs(conn: Connection, experiment: Any) -> list[tuple[str, dict[str, str]]]:
    """Each primary review whose variant and control both finished, with their runs."""
    found: dict[str, dict[str, str]] = {}
    for row in conn.execute(select(eval_arms.c.primary_run_id, eval_arms.c.label, eval_arms.c.arm_run_id).where(
            eval_arms.c.experiment_id == experiment.id, eval_arms.c.status == "done",
            eval_arms.c.arm_run_id.is_not(None))):
        found.setdefault(row.primary_run_id, {})[row.label] = row.arm_run_id
    return sorted((p, r) for p, r in found.items() if set(r) == {VARIANT, CONTROL})


def _step_cost(conn: Connection, run_id: str, step: str | None) -> tuple[float | None, float | None]:
    if step is None:
        return None, None
    cost = conn.execute(select(func.sum(llm_calls.c.cost_usd)).where(
        llm_calls.c.run_id == run_id, llm_calls.c.step_key == step, llm_calls.c.cached.is_(False))).scalar()
    timed = conn.execute(select(run_steps.c.started_at, run_steps.c.finished_at).where(
        run_steps.c.run_id == run_id, run_steps.c.step_key == step)).first()
    took = (timed.finished_at - timed.started_at).total_seconds() \
        if timed is not None and timed.started_at and timed.finished_at else None
    return (float(cost) if cost is not None else None), took


def _target(conn: Connection, run_id: str) -> str | None:
    inputs = conn.execute(select(jobs.c.inputs).where(jobs.c.run_id == run_id, jobs.c.resume_of_gate.is_(None))
                          .limit(1)).scalar()
    if isinstance(inputs, dict) and isinstance(inputs.get("slug"), str) and inputs.get("number") is not None:
        return f"{inputs['slug']}#{inputs['number']}"
    return None


def _review(conn: Connection, experiment: Any, primary: str, arm_runs: dict[str, str]) -> dict[str, Any] | None:
    """One pull request's issues, and what each label raised, found, and spent there. None while a finding is
    unjudged."""
    runs = {PRIMARY: primary, **arm_runs}
    found = {label: arm_matching.candidates_of(conn, run, experiment.specialist) for label, run in runs.items()}
    ids = [c.id for cs in found.values() for c in cs]
    valid = dict(conn.execute(select(eval_judgments.c.candidate_id, eval_judgments.c.valid).where(
        eval_judgments.c.experiment_id == experiment.id, eval_judgments.c.candidate_id.in_(ids))).all()) if ids else {}
    if len(valid) < len(ids):
        return None
    matched = dict(conn.execute(select(eval_matches.c.left_candidate_id, eval_matches.c.right_candidate_id).where(
        eval_matches.c.experiment_id == experiment.id, eval_matches.c.primary_run_id == primary,
        eval_matches.c.matcher_version == arm_matching.MATCHER_VERSION)).all())

    def issue_of(candidate_id: str) -> str:
        # A variant finding may match a control finding that matched nothing, which then names the issue.
        seen = candidate_id
        while matched.get(seen) is not None and matched[seen] != seen:
            seen = matched[seen]
        return seen

    issues: dict[str, set[str]] = {}
    real: dict[str, bool] = {}
    for label, cs in found.items():
        for c in cs:
            issue = issue_of(c.id)
            issues.setdefault(issue, set()).add(label)
            real[issue] = real.get(issue, False) or valid[c.id]
    spend = {label: _step_cost(conn, run, arms.review_step(conn, run, experiment.specialist))
             for label, run in runs.items()}
    real_count = sum(real.values())
    per: dict[str, dict[str, Any]] = {}
    for label, cs in found.items():
        hit = sum(1 for i, ls in issues.items() if real[i] and label in ls)
        good = sum(valid[c.id] for c in cs)
        per[label] = {"raised": len(cs), "valid": good, "found": hit,
                      "recall": hit / real_count if real_count else None,
                      "precision": good / len(cs) if cs else None,
                      "unique_valid": sum(1 for i, ls in issues.items() if real[i] and ls == {label}),
                      "cost_usd": spend[label][0], "latency_s": spend[label][1]}
    return {"primary_run_id": primary, "pr": _target(conn, primary), "issues": issues, "real": real, "per": per}


def _mean(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return mean(known) if known else None


def difference(reviews: list[dict[str, Any]], measure: str, a: str, b: str) -> dict[str, Any]:
    """The per pull request difference a minus b on ``measure``: its mean, standard error, count, and whether it
    counts, more than SE_MULTIPLE standard errors from zero."""
    diffs = [r["per"][a][measure] - r["per"][b][measure] for r in reviews
             if r["per"][a][measure] is not None and r["per"][b][measure] is not None]
    if not diffs:
        return {"mean": None, "se": None, "n": 0, "significant": False}
    average = mean(diffs)
    se = stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else None
    significant = se is not None and abs(average) > SE_MULTIPLE * se
    return {"mean": average, "se": se, "n": len(diffs), "significant": significant}


def compute(conn: Connection, experiment: Any) -> dict[str, Any]:
    """Every label's measures over the pull requests whose arms both finished and whose findings are all judged,
    the effect (variant minus control), and the environment shift (control minus primary)."""
    reviews = [r for r in (_review(conn, experiment, p, runs) for p, runs in _pairs(conn, experiment)) if r]
    real_total = sum(sum(r["real"].values()) for r in reviews)
    labels = {}
    for label in EVAL_SCORE_LABELS:
        raised = sum(r["per"][label]["raised"] for r in reviews)
        found = sum(r["per"][label]["found"] for r in reviews)
        labels[label] = {
            "prs": len(reviews), "found": found, "raised": raised,
            "valid": sum(r["per"][label]["valid"] for r in reviews),
            "recall": found / real_total if real_total else None,
            "precision": sum(r["per"][label]["valid"] for r in reviews) / raised if raised else None,
            "unique_valid": sum(r["per"][label]["unique_valid"] for r in reviews),
            "cost_usd": _mean(r["per"][label]["cost_usd"] for r in reviews),
            "latency_s": _mean(r["per"][label]["latency_s"] for r in reviews)}
    test = {"effect": {m: difference(reviews, m, VARIANT, CONTROL) for m in MEASURES},
            "shift": {m: difference(reviews, m, CONTROL, PRIMARY) for m in MEASURES}}
    return {"prs": len(reviews), "real": real_total, "labels": labels, "test": test, "reviews": reviews}


def store(conn: Connection, experiment: Any, computed: dict[str, Any], now: datetime) -> None:
    """Keep each label's measures. The test, effect and shift, rides on every row in the noise_floor column."""
    conn.execute(delete(eval_scores).where(eval_scores.c.experiment_id == experiment.id,
                                           eval_scores.c.scorer_version == SCORER_VERSION))
    conn.execute(insert(eval_scores), [
        {"id": new_id(), "team_id": experiment.team_id, "experiment_id": experiment.id, "arm_label": label,
         "scorer_version": SCORER_VERSION, "prs": m["prs"], "recall": m["recall"], "precision": m["precision"],
         "unique_valid": m["unique_valid"], "cost_usd": m["cost_usd"], "latency_s": m["latency_s"],
         "noise_floor": computed["test"], "computed_at": now}
        for label, m in computed["labels"].items()])


def stored(conn: Connection, experiment_ids: list[str]) -> dict[str, tuple[dict[str, dict[str, Any]], dict[str, Any]]]:
    """Each experiment's stored measures by label, in compute's shape, and its stored test."""
    found: dict[str, tuple[dict[str, dict[str, Any]], dict[str, Any]]] = {}
    if not experiment_ids:
        return found
    for row in conn.execute(select(eval_scores).where(eval_scores.c.experiment_id.in_(experiment_ids),
                                                      eval_scores.c.scorer_version == SCORER_VERSION)):
        labels, _ = found.setdefault(row.experiment_id, ({}, row.noise_floor or {}))
        labels[row.arm_label] = {
            "prs": row.prs, "recall": row.recall, "precision": row.precision, "unique_valid": row.unique_valid,
            "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None, "latency_s": row.latency_s}
    return found


def cost_share(labels: dict[str, dict[str, Any]]) -> float | None:
    """The variant's mean cost as a share of the control's, which ran the current setting under the same
    conditions."""
    v, c = labels[VARIANT]["cost_usd"], labels[CONTROL]["cost_usd"]
    return v / c if v is not None and c else None


def verdict(experiment: Any, labels: dict[str, dict[str, Any]] | None,
            test: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the comparison shows, as a kind and words: too early, within normal variation, better, cheaper, or
    worse. Only a difference that counts moves it off within normal variation."""
    prs = labels[PRIMARY]["prs"] if labels and PRIMARY in labels else 0
    if not labels or set(labels) != set(EVAL_SCORE_LABELS) or prs < experiment.min_prs:
        return {"kind": "early", "words": EARLY, "prs": prs, "cost_share": None}
    effect = (test or {}).get("effect") or {}

    def counts(measure: str, sign: int) -> bool:
        found = effect.get(measure) or {}
        return bool(found.get("significant")) and found.get("mean") is not None and found["mean"] * sign > 0

    share = cost_share(labels)
    shown = f" at {round(share * 100)}% of the cost" if share is not None else ""
    if counts("recall", -1):
        kind, words = "worse", "Found fewer real issues"
    elif counts("recall", 1):
        kind, words = "better", f"Found more real issues{shown}"
    elif counts("cost_usd", -1) and not counts("precision", -1):
        kind, words = "cheaper", f"Found as many real issues, within normal variation{',' + shown if shown else ''}"
    else:
        kind, words = "within", WITHIN
    return {"kind": kind, "words": words, "prs": prs, "cost_share": share}


def change(experiment: Any) -> dict[str, Any]:
    alias = experiment.variant.get("alias")
    return {"specialist": experiment.specialist, experiment.variable: experiment.variant[experiment.variable],
            **({"alias": alias} if alias else {})}


def sentence(experiment: Any, computed: dict[str, Any], kind: str) -> str:
    """The recommendation in words, such as "Over 40 pull requests, concurrency at high effort found as much as
    xhigh found at 60% of the cost". It says more only when recall is higher by a difference that counts."""
    labels = computed["labels"]
    variant, current = sides(experiment)
    share = cost_share(labels)
    cost = f" at {round(share * 100)}% of the cost" if share is not None else ""
    lead = f"Over {computed['prs']} pull requests, {specialist_name(experiment.specialist)} {variant}"
    if kind != "better":
        return f"{lead} found as much as {current} found{cost}."
    more = labels[VARIANT]["found"] - labels[CONTROL]["found"]
    issues = "real issue" if more == 1 else "real issues"
    return f"{lead} found {more} more {issues} than {current}{cost}."


def recommend(engine: Engine, team_id: str, now: datetime) -> int:
    """Write a recommendation for each open experiment whose result counts and favors the variant, unless one is
    waiting or was dismissed. Returns how many it wrote."""
    written = 0
    with engine.begin() as conn:
        experiments = conn.execute(select(eval_experiments).where(eval_experiments.c.team_id == team_id,
                                                                  eval_experiments.c.status != "finished")
                                   .order_by(eval_experiments.c.created_at, eval_experiments.c.id)).all()
        for experiment in experiments:
            if conn.execute(select(eval_recommendations.c.id).where(
                    eval_recommendations.c.experiment_id == experiment.id,
                    eval_recommendations.c.state.in_(DECIDED))).first() is not None:
                continue
            computed = compute(conn, experiment)
            result = verdict(experiment, computed["labels"], computed["test"])
            if result["kind"] not in RECOMMENDS:
                continue
            evidence = {"prs": computed["prs"], "real": computed["real"], "result": result["kind"],
                        "scorer_version": SCORER_VERSION, "effect": computed["test"]["effect"],
                        "environment_shift": computed["test"]["shift"],
                        "labels": computed["labels"]}
            conn.execute(insert(eval_recommendations).values(
                id=new_id(), team_id=team_id, experiment_id=experiment.id, specialist=experiment.specialist,
                change=change(experiment), sentence=sentence(experiment, computed, result["kind"]),
                evidence=evidence, state="waiting", created_at=now))
            written += 1
    return written


# What the Models screen shows

def _side(name: str, m: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, **{k: m[k] for k in ("raised", "valid", "found", "recall", "precision", "unique_valid",
                                                "cost_usd", "latency_s")}}


def summary(row: Any, labels: dict[str, dict[str, Any]] | None, test: dict[str, Any] | None = None) -> dict[str, Any]:
    result = verdict(row, labels, test)
    return {"id": row.id, "specialist": row.specialist, "variable": row.variable, "question": question(row),
            "status": row.status, "baseline": row.baseline, "variant": row.variant, "prs": result["prs"],
            "min_prs": row.min_prs, "result": {"kind": result["kind"], "words": result["words"]},
            "cost_share": result["cost_share"]}


def comparisons(conn: Connection, team_id: str, specialist: str | None = None) -> list[dict[str, Any]]:
    """One row per experiment, newest first: its question, how many pull requests it compared, and the result."""
    query = select(eval_experiments).where(eval_experiments.c.team_id == team_id)
    if specialist is not None:
        query = query.where(eval_experiments.c.specialist == specialist)
    rows = conn.execute(query.order_by(eval_experiments.c.created_at.desc(), eval_experiments.c.id)).all()
    scores = stored(conn, [r.id for r in rows])
    return [summary(row, *scores.get(row.id, (None, None))) for row in rows]


def can_approve(change: dict[str, Any], *, lead: bool, admin: bool) -> bool:
    """Whether the viewer may approve a change: an effort change takes a team lead, a model change an admin."""
    return admin or (lead and "effort" in (change or {}))


def recommendation(row: Any) -> dict[str, Any]:
    return {"id": row.id, "experiment_id": row.experiment_id, "specialist": row.specialist, "sentence": row.sentence,
            "change": row.change, "evidence": row.evidence, "state": row.state,
            "created_at": row.created_at.isoformat(),
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "applied_version": row.applied_version}


def recommendations(conn: Connection, team_id: str, specialist: str | None = None) -> list[dict[str, Any]]:
    """The recommendations waiting for a team lead, oldest first."""
    query = select(eval_recommendations).where(eval_recommendations.c.team_id == team_id,
                                               eval_recommendations.c.state == "waiting")
    if specialist is not None:
        query = query.where(eval_recommendations.c.specialist == specialist)
    return [recommendation(r) for r in conn.execute(query.order_by(eval_recommendations.c.created_at,
                                                                   eval_recommendations.c.id))]


def detail(conn: Connection, experiment: Any) -> dict[str, Any]:
    """One comparison of the variant with the control, which kept the current setting under the same conditions: what
    both found, what only one found, how much was real, cost and time per side, the effect and the environment
    shift, and each pull request's point for the dot plot."""
    computed = compute(conn, experiment)
    labels, reviews = computed["labels"], computed["reviews"]
    result = verdict(experiment, labels, computed["test"])
    variant, current = sides(experiment)
    buckets = {"both": 0, "only_current": 0, "only_variant": 0}
    real = dict.fromkeys(buckets, 0)
    for r in reviews:
        for issue, ls in r["issues"].items():
            key = "both" if {CONTROL, VARIANT} <= ls else "only_current" if CONTROL in ls \
                else "only_variant" if VARIANT in ls else None
            if key:
                buckets[key] += 1
                real[key] += r["real"][issue]
    states = dict(conn.execute(select(eval_arms.c.status, func.count()).where(
        eval_arms.c.experiment_id == experiment.id).group_by(eval_arms.c.status)).all())

    def point(r: dict[str, Any], label: str) -> dict[str, Any]:
        return {k: r["per"][label][k] for k in ("cost_usd", "latency_s", "raised", "valid")}

    return {"comparison": summary(experiment, labels, computed["test"]), "result": result,
            "prs": computed["prs"], "found": buckets, "valid": real, "effect": computed["test"]["effect"],
            "environment_shift": computed["test"]["shift"],
            "sides": {"current": _side(current, labels[CONTROL]),
                      "variant": _side(variant.split(" ", 1)[1], labels[VARIANT]),
                      "review": _side(current, labels[PRIMARY])},
            "points": [{"primary_run_id": r["primary_run_id"], "pr": r["pr"], "current": point(r, CONTROL),
                        "variant": point(r, VARIANT)} for r in reviews],
            "arms": {s: int(states.get(s, 0)) for s in ("queued", "running", "done", "failed", "skipped")}}
