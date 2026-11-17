"""Match each comparison run's findings to the primary review's, so the scorer knows which findings repeat.

Only the experiment's specialist is compared. A pair needs equal finding keys, or the same path, lines within
LINE_WINDOW, and enough shared claim words. Each primary finding takes at most one finding of each arm, best score
first. Then the variant's findings the primary missed pair the same way with the control's it missed, so an issue
only the two comparison runs found counts once. A finding that pairs with nothing is stored with method ``none``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, and_, delete, exists, insert, select

from armature.server.db import new_id
from armature.server.db.schema import eval_arms, eval_matches, finding_candidates
from armature.server.insights import arms
from armature.server.insights.candidates import MIN_OVERLAP, _near, overlap

MATCHER_VERSION = "2"
ENDED = ("done", "failed", "skipped")


def candidates_of(conn: Connection, run_id: str, specialist: str) -> list[Any]:
    """The run's candidates from the specialist's step in its first review pass."""
    step = arms.review_step(conn, run_id, specialist)
    if step is None:
        return []
    return conn.execute(select(finding_candidates).where(finding_candidates.c.run_id == run_id,
                                                         finding_candidates.c.step_key == step)
                        .order_by(finding_candidates.c.ordinal)).all()


def _score(left: Any, right: Any) -> tuple[str, float] | None:
    if left.finding_key and left.finding_key == right.finding_key:
        return "key", 1.0
    if not left.path or left.path != right.path or not _near(left, right):
        return None
    score = overlap(left.claim or left.body, right.claim or right.body)
    return ("heuristic", score) if score >= MIN_OVERLAP else None


def assign(left: list[Any], right: list[Any]) -> list[tuple[Any, Any | None, str, float | None]]:
    """Pair each left finding with at most one right finding and the reverse, best pairs first: key matches, then
    higher scores, then the earlier findings."""
    scored = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            found = _score(a, b)
            if found:
                scored.append((found[0] != "key", -found[1], i, j, found))
    taken_left: dict[int, tuple[Any, str, float]] = {}
    taken_right: set[int] = set()
    for _, _, i, j, (method, score) in sorted(scored, key=lambda s: s[:4]):
        if i not in taken_left and j not in taken_right:
            taken_left[i] = (right[j], method, score)
            taken_right.add(j)
    return [(a, *taken_left[i]) if i in taken_left else (a, None, "none", None) for i, a in enumerate(left)]


def match(conn: Connection, experiment: Any, primary_run_id: str) -> int:
    """Store the matches of every ended arm of the primary review under MATCHER_VERSION. Returns how many."""
    arm_runs = dict(conn.execute(select(eval_arms.c.label, eval_arms.c.arm_run_id).where(
        eval_arms.c.experiment_id == experiment.id, eval_arms.c.primary_run_id == primary_run_id,
        eval_arms.c.status == "done", eval_arms.c.arm_run_id.is_not(None))).all())
    primary = candidates_of(conn, primary_run_id, experiment.specialist)
    conn.execute(delete(eval_matches).where(eval_matches.c.experiment_id == experiment.id,
                                            eval_matches.c.primary_run_id == primary_run_id,
                                            eval_matches.c.matcher_version == MATCHER_VERSION))
    found: dict[str, list[tuple[Any, Any, str, float | None]]] = {
        label: assign(candidates_of(conn, run_id, experiment.specialist), primary)
        for label, run_id in sorted(arm_runs.items())}
    if {"variant", "control"} <= set(found):
        missed = {label: [m[0] for m in found[label] if m[1] is None] for label in ("variant", "control")}
        joined = {left.id: (right, method, score) for left, right, method, score
                  in assign(missed["variant"], missed["control"]) if right is not None}
        found["variant"] = [(left, *joined[left.id]) if left.id in joined else (left, right, method, score)
                            for left, right, method, score in found["variant"]]
    rows = [{"id": new_id(), "team_id": experiment.team_id, "experiment_id": experiment.id,
             "primary_run_id": primary_run_id, "left_candidate_id": left.id,
             "right_candidate_id": right.id if right is not None else None, "method": method, "score": score,
             "matcher_version": MATCHER_VERSION}
            for pairs in found.values() for left, right, method, score in pairs]
    if rows:
        conn.execute(insert(eval_matches), rows)
    return len(rows)


def pending(conn: Connection, experiment: Any) -> list[str]:
    """The primary reviews whose arms all ended and hold a finding of the specialist not matched yet."""
    arm = eval_arms.alias("arm")
    open_arm = exists().where(arm.c.experiment_id == experiment.id, arm.c.primary_run_id == eval_arms.c.primary_run_id,
                              arm.c.status.not_in(ENDED))
    unmatched = exists().where(
        finding_candidates.c.run_id == eval_arms.c.arm_run_id, finding_candidates.c.specialist == experiment.specialist,
        ~exists().where(eval_matches.c.experiment_id == experiment.id,
                        eval_matches.c.left_candidate_id == finding_candidates.c.id,
                        eval_matches.c.matcher_version == MATCHER_VERSION))
    return list(conn.execute(select(eval_arms.c.primary_run_id).distinct().where(
        and_(eval_arms.c.experiment_id == experiment.id, eval_arms.c.status == "done", ~open_arm, unmatched))
        .order_by(eval_arms.c.primary_run_id)).scalars())
