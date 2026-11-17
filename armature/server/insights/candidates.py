"""Finding candidates: store each finding a specialist raised, and match it to the lead decision it became.

The lead rewords and merges what specialists say, so equal finding keys match only when it kept a claim verbatim.
Otherwise a candidate matches a decision on the same path near the same line that names its specialist in raised_by
and shares enough of its words. Either way the decision must come from the candidate's own round, the step that
called its workflow. A candidate nothing matches gets one row with method ``none``.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, Text, cast, delete, exists, insert, select

from armature.server import ingest, risk
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    candidate_matches,
    finding_candidates,
    findings,
    run_steps,
    runs,
    tool_calls,
)

log = logging.getLogger(__name__)

MATCHER_VERSION = "1"
LINE_WINDOW = 15
# Overlap of the smaller word set, not Jaccard: a lead's claim merges several specialists' findings, so it holds most
# of each one's words plus others, and Jaccard punishes those extras.
MIN_OVERLAP = 0.4
SPECIALIST_STEPS = (*risk.SPECIALISTS, risk.REVIEWER)
BATCH = 50
DECISIONS_TOOL = "review.decisions"
WORD = re.compile(r"[a-z0-9]+")
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could", "do", "does", "for", "from", "has",
    "have", "if", "in", "into", "is", "it", "its", "may", "might", "no", "not", "of", "on", "or", "s", "should", "so",
    "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "those", "to", "was", "were",
    "when", "which", "will", "with", "would"})


def insert_candidates(conn: Connection, run_id: str, rows: Iterable[dict[str, Any]]) -> int:
    """Insert the run's candidates whose step and ordinal aren't stored yet, the first of any repeat winning.
    Returns how many it added."""
    new: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        new.setdefault((row["step_key"], row["ordinal"]), row)
    if not new:
        return 0
    stored = {(r.step_key, r.ordinal) for r in conn.execute(
        select(finding_candidates.c.step_key, finding_candidates.c.ordinal).where(
            finding_candidates.c.run_id == run_id, finding_candidates.c.step_key.in_({key for key, _ in new})))}
    added = [{"id": new_id(), "run_id": run_id, **row} for key, row in new.items() if key not in stored]
    if added:
        # A comparison run's candidates carry its arm, so no insights measure counts them.
        ref = conn.execute(select(runs.c.trigger_ref).where(runs.c.id == run_id)).scalar()
        arm_id = ref.get("arm_id") if isinstance(ref, dict) else None
        conn.execute(insert(finding_candidates),
                     [{**row, "arm_id": arm_id if isinstance(arm_id, str) else None} for row in added])
    return len(added)


def store(conn: Connection, run_id: str, rows: list[dict[str, Any]]) -> int:
    """insert_candidates under a savepoint, so a failure costs only these candidates and never the caller's batch."""
    try:
        with conn.begin_nested():
            return insert_candidates(conn, run_id, rows)
    except Exception:
        # Candidates are derived from step outputs too, and backfill-candidates stores what this missed.
        log.exception("could not store the candidates of run %s", run_id)
        return 0


def decisions_recorded(run_id: Any) -> Any:
    """Whether the run's lead recorded its decisions for real: a live review.decisions call that succeeded."""
    return exists().where(tool_calls.c.run_id == run_id, tool_calls.c.tool == DECISIONS_TOOL,
                          tool_calls.c.mode == "live", tool_calls.c.outcome == "ok")


def _round(step_key: str | None) -> str | None:
    # The step that called the workflow a step ran in, such as `first` for `first/correctness`.
    return step_key.rsplit("/", 1)[0] if step_key and "/" in step_key else None


def _words(text: str | None) -> set[str]:
    return {w for w in WORD.findall((text or "").lower()) if w not in STOPWORDS}


def overlap(a: str | None, b: str | None) -> float:
    """The share of the smaller text's words the other text holds, ignoring case, punctuation, and stopwords."""
    left, right = _words(a), _words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _near(candidate: Any, finding: Any) -> bool:
    # A decision often has no line, so only two known lines can rule a match out.
    return candidate.line is None or finding.line is None or abs(candidate.line - finding.line) <= LINE_WINDOW


def _matches(candidate: Any, decided: list[Any]) -> list[tuple[Any, str, float]]:
    # A decision recorded without its round, as before decisions named their parent step, may be any round's.
    decided = [f for f in decided if _round(f.step_id) is None or _round(f.step_id) == _round(candidate.step_key)]
    keyed = [(f, "key", 1.0) for f in decided if candidate.finding_key and f.finding_key == candidate.finding_key]
    if keyed:
        return keyed
    best: tuple[Any, str, float] | None = None
    for finding in decided:
        if (candidate.specialist not in (finding.raised_by or []) or not candidate.path
                or finding.path != candidate.path or not _near(candidate, finding)):
            continue
        score = overlap(candidate.claim or candidate.body, " ".join(filter(None, (finding.title, finding.body))))
        if score >= MIN_OVERLAP and (best is None or score > best[2]):
            best = (finding, "heuristic", score)
    return [best] if best else []


def match_run(conn: Connection, run_id: str, now: datetime | None = None) -> dict[str, int]:
    """Match every candidate of the run again under MATCHER_VERSION. Returns how many matched by each method.

    A run whose lead never recorded decisions for real gets no matches, since none of its candidates was dropped.
    """
    if not conn.execute(select(decisions_recorded(run_id))).scalar():
        return {}
    candidates = conn.execute(select(finding_candidates).where(finding_candidates.c.run_id == run_id)
                              .order_by(finding_candidates.c.step_key, finding_candidates.c.ordinal)).all()
    if not candidates:
        return {}
    # A fixed row repeats a decision the run already recorded.
    decided = conn.execute(select(findings).where(findings.c.run_id == run_id,
                                                  findings.c.decision.is_distinct_from("fixed"))
                           .order_by(findings.c.created_at, findings.c.id)).all()
    conn.execute(delete(candidate_matches).where(
        candidate_matches.c.candidate_id.in_(select(finding_candidates.c.id)
                                             .where(finding_candidates.c.run_id == run_id)),
        candidate_matches.c.matcher_version == MATCHER_VERSION))
    now = now or utcnow()
    counts: dict[str, int] = {}
    rows = []
    for candidate in candidates:
        found = _matches(candidate, decided) or [(None, "none", None)]
        counts[found[0][1]] = counts.get(found[0][1], 0) + 1
        rows += [{"id": new_id(), "team_id": candidate.team_id, "candidate_id": candidate.id,
                  "finding_id": finding.id if finding is not None else None, "match_ordinal": ordinal,
                  "method": method, "score": score, "matcher_version": MATCHER_VERSION, "created_at": now}
                 for ordinal, (finding, method, score) in enumerate(found)]
    conn.execute(insert(candidate_matches), rows)
    return counts


def rematch(conn: Connection, run_id: str) -> None:
    """Match the run's candidates again after it recorded decisions, never failing the caller's transaction."""
    try:
        with conn.begin_nested():
            match_run(conn, run_id)
    except Exception:
        # Matches are derived, and backfill-candidates rebuilds them, so a failure must not cost the decisions.
        log.exception("could not match the candidates of run %s", run_id)


def outputs(step: Any) -> list[tuple[str, dict[str, Any]]]:
    """Each (step key, output) a stored step holds: one, or one per fan-out branch, keyed as ingest keys them."""
    if isinstance(step.output, dict):
        return [(step.step_key, step.output)]
    if isinstance(step.output, list):
        return [(f"{step.step_key}#{branch}", output) for branch, output in enumerate(step.output)
                if isinstance(output, dict)]
    return []


def from_output(team_id: str, project_id: str | None, key: str, step: Any, output: dict[str, Any],
                now: datetime) -> list[dict[str, Any]]:
    """The candidate rows of a specialist step's stored output, as its candidate events would have made them."""
    found = output.get("findings")
    if not isinstance(found, list):
        return []
    rows = []
    for ordinal, item in enumerate(found[:ingest.MAX_CANDIDATES]):
        if isinstance(item, dict):
            row = ingest.candidate_row(team_id, project_id, key, step.step_id, step.finished_at or now,
                                       {**item, "ordinal": ordinal, "specialist": step.step_id})
            if row is not None:
                rows.append(row)
    return rows


def backfill(engine: Engine, *, batch: int = BATCH, now: datetime | None = None) -> dict[str, int]:
    """Store the candidates of the specialist steps of every ended run whose lead recorded decisions, from their
    outputs, then match each run.

    A step whose output the emitter cut to fit an event is skipped and counted, since its findings may be cut too.
    Safe to rerun: stored candidates are skipped and matches are rebuilt.
    """
    from armature.server.history import OPEN_RUNS

    now = now or utcnow()
    counts = {"runs": 0, "steps": 0, "truncated": 0, "candidates": 0, "key": 0, "heuristic": 0, "none": 0}
    specialist = run_steps.c.step_id.in_(SPECIALIST_STEPS)
    last = ""
    while True:
        with engine.begin() as conn:
            listed = conn.execute(
                select(runs.c.id, runs.c.team_id, runs.c.project_id)
                .where(runs.c.id > last, runs.c.status.not_in((*OPEN_RUNS, "lost")),
                       exists().where(run_steps.c.run_id == runs.c.id, specialist), decisions_recorded(runs.c.id))
                .order_by(runs.c.id).limit(batch)).all()
            if not listed:
                return counts
            by_run: dict[str, list[Any]] = {}
            for step in conn.execute(
                    select(run_steps.c.run_id, run_steps.c.step_key, run_steps.c.step_id, run_steps.c.finished_at,
                           run_steps.c.output)
                    .where(run_steps.c.run_id.in_([r.id for r in listed]), specialist,
                           cast(run_steps.c.output, Text).like('%"findings"%'))):
                by_run.setdefault(step.run_id, []).append(step)
            for run in listed:
                rows = []
                for step in by_run.get(run.id, []):
                    for key, output in outputs(step):
                        if output.get(ingest.TRUNCATED):
                            counts["truncated"] += 1
                            continue
                        counts["steps"] += 1
                        rows += from_output(run.team_id, run.project_id, key, step, output, now)
                counts["candidates"] += insert_candidates(conn, run.id, rows)
                matched = match_run(conn, run.id, now)
                counts["runs"] += bool(matched)
                for method, n in matched.items():
                    counts[method] += n
        last = listed[-1].id
        if len(listed) < batch:
            return counts

