"""PR insights: matching candidates to decisions, the backfill from step outputs, the taxonomy, and labeling."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select

from armature.server import ingest, llm
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    candidate_matches,
    finding_candidates,
    findings,
    labels,
    providers,
    run_steps,
    runs,
    settings,
    taxonomies,
    tool_calls,
)
from armature.server.insights import candidates, labeling, taxonomy
from armature.server.tools.review import finding_key
from tests.server.live_support import add_team

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CACHE = "the cache is never cleared on sign-out; nothing calls signedOut()"
LATER = datetime(2026, 11, 13, 12, 1, tzinfo=UTC)
LEAD = "The cache is never cleared on sign-out, so the next user sees the previous user's memories."


@pytest.fixture
def team(sqlite_engine):
    return add_team(sqlite_engine, lambda: NOW, "mobile")


def _run(conn, team, status="succeeded", decided=("live", "ok")):
    run_id = new_id()
    conn.execute(insert(runs).values(id=run_id, team_id=team[0], project_id=team[1], status=status, started_at=NOW))
    if decided:
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=team[0], project_id=team[1],
                                               step_id="decisions", tool="review.decisions", mode=decided[0],
                                               outcome=decided[1], created_at=NOW))
    return run_id


def _candidate(conn, team, run_id, step_key, ordinal, specialist, path, line, claim, at=NOW):
    conn.execute(insert(finding_candidates).values(
        id=new_id(), team_id=team[0], project_id=team[1], run_id=run_id, step_key=step_key, specialist=specialist,
        ordinal=ordinal, finding_key=finding_key({"path": path, "claim": claim}), path=path, line=line, claim=claim,
        created_at=at))


def _finding(conn, run_id, path, claim, raised_by, decision="block", line=None, key=None, at=NOW, step="decisions"):
    finding_id = new_id()
    conn.execute(insert(findings).values(id=finding_id, run_id=run_id, finding_key=key or new_id()[:12], path=path,
                                         line=line, body=claim, raised_by=raised_by, decision=decision,
                                         step_id=step, created_at=at))
    return finding_id


def _matches(engine, run_id):
    with engine.connect() as conn:
        return sorted((r.specialist, r.ordinal, r.match_ordinal, r.method, r.finding_id) for r in conn.execute(
            select(finding_candidates.c.specialist, finding_candidates.c.ordinal, candidate_matches.c.match_ordinal,
                   candidate_matches.c.method, candidate_matches.c.finding_id)
            .join(candidate_matches, candidate_matches.c.candidate_id == finding_candidates.c.id)
            .where(finding_candidates.c.run_id == run_id)))


def test_the_matcher_needs_the_specialist_the_path_a_near_line_and_shared_words(sqlite_engine, team):
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "correctness", 0, "correctness", "M.kt", 7, CACHE)
        _candidate(conn, team, run_id, "correctness", 1, "correctness", "M.kt", 40, CACHE)
        _candidate(conn, team, run_id, "security", 0, "security", "M.kt", 7, CACHE)
        _candidate(conn, team, run_id, "performance", 0, "performance", "M.kt", 7, "the list renders every row twice")
        _candidate(conn, team, run_id, "simplicity", 0, "simplicity", "A.kt", 1, "drop the helper")
        lead = _finding(conn, run_id, "M.kt", LEAD, ["correctness", "performance"], line=8)
        first = _finding(conn, run_id, "A.kt", "drop the helper", ["simplicity"], decision="drop",
                         key=finding_key({"path": "A.kt", "claim": "drop the helper"}))
        second = _finding(conn, run_id, "A.kt", "drop the helper", ["simplicity"], decision="suggest",
                          key=finding_key({"path": "A.kt", "claim": "drop the helper"}), at=LATER)
        _finding(conn, run_id, "A.kt", "drop the helper", ["simplicity"], decision="fixed",
                 key=finding_key({"path": "A.kt", "claim": "drop the helper"}))
        assert candidates.match_run(conn, run_id, NOW) == {"heuristic": 1, "none": 3, "key": 1}
        assert candidates.match_run(conn, run_id, NOW) == {"heuristic": 1, "none": 3, "key": 1}
    assert _matches(sqlite_engine, run_id) == sorted([
        ("correctness", 0, 0, "heuristic", lead), ("correctness", 1, 0, "none", None),
        ("security", 0, 0, "none", None), ("performance", 0, 0, "none", None),
        ("simplicity", 0, 0, "key", first), ("simplicity", 0, 1, "key", second)])


def test_a_reworded_claim_matches_the_merged_decision_even_when_the_decision_has_no_line(sqlite_engine, team):
    reworded = "one user's memories can show to the next user on a shared device."
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "security", 0, "security", "M.kt", 9, reworded)
        _candidate(conn, team, run_id, "security", 1, "security", "M.kt", 400, reworded)
        _candidate(conn, team, run_id, "security", 2, "security", "N.kt", 9, reworded)
        lead = _finding(conn, run_id, "M.kt", LEAD, ["correctness", "security"])
        assert candidates.match_run(conn, run_id, NOW) == {"heuristic": 2, "none": 1}
        score = conn.execute(select(candidate_matches.c.score).where(candidate_matches.c.method == "heuristic")
                             .limit(1)).scalar()
    assert score == pytest.approx(3 / 7)
    assert _matches(sqlite_engine, run_id) == sorted([
        ("security", 0, 0, "heuristic", lead), ("security", 1, 0, "heuristic", lead),
        ("security", 2, 0, "none", None)])


def test_the_matcher_prefers_the_key_then_the_best_overlap_within_fifteen_lines(sqlite_engine, team):
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "correctness", 0, "correctness", "M.kt", 10, CACHE)
        _candidate(conn, team, run_id, "correctness", 1, "correctness", "M.kt", 10, "the cache leaks the token")
        _candidate(conn, team, run_id, "correctness", 2, "correctness", "M.kt", 41, "the cache leaks the token")
        keyed = _finding(conn, run_id, "M.kt", "reworded entirely", ["security"],
                         key=finding_key({"path": "M.kt", "claim": CACHE}))
        _finding(conn, run_id, "M.kt", CACHE, ["correctness"], line=10)
        _finding(conn, run_id, "M.kt", "the cache grows", ["correctness"], line=25)
        best = _finding(conn, run_id, "M.kt", "the cache leaks the session token", ["correctness"], line=25)
        edge = _finding(conn, run_id, "M.kt", "the cache leaks the token", ["correctness"], line=26)
        assert candidates.match_run(conn, run_id, NOW) == {"key": 1, "heuristic": 2}
    # Line 10 is 15 from best and 16 from edge, and line 41 is 16 from best and 15 from edge.
    assert _matches(sqlite_engine, run_id) == sorted([
        ("correctness", 0, 0, "key", keyed), ("correctness", 1, 0, "heuristic", best),
        ("correctness", 2, 0, "heuristic", edge)])


def test_each_round_matches_only_its_own_decisions(sqlite_engine, team):
    kept = "the token is logged at debug level"
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "first/security", 0, "security", "M.kt", 7, CACHE)
        _candidate(conn, team, run_id, "first/security", 1, "security", "N.kt", 3, kept)
        _candidate(conn, team, run_id, "second/security", 0, "security", "M.kt", 7, CACHE)
        first = _finding(conn, run_id, "M.kt", CACHE, ["security"], key=finding_key({"path": "M.kt", "claim": CACHE}),
                         step="first/decisions")
        _finding(conn, run_id, "N.kt", kept, ["security"], key=finding_key({"path": "N.kt", "claim": kept}),
                 step="second/decisions")
        assert candidates.match_run(conn, run_id, NOW) == {"key": 1, "none": 2}
        assert _matches(sqlite_engine, run_id) == []
    assert _matches(sqlite_engine, run_id) == sorted([
        ("security", 0, 0, "key", first), ("security", 1, 0, "none", None), ("security", 0, 0, "none", None)])
    with sqlite_engine.begin() as conn:
        legacy = _finding(conn, run_id, "N.kt", kept, ["security"], key=finding_key({"path": "N.kt", "claim": kept}))
        assert candidates.match_run(conn, run_id, NOW) == {"key": 2, "none": 1}
    assert ("security", 1, 0, "key", legacy) in _matches(sqlite_engine, run_id)


@pytest.mark.parametrize("decided", [None, ("dry", "ok"), ("live", "failed"), ("live", "refused")])
def test_a_run_without_a_live_decisions_call_gets_no_matches(sqlite_engine, team, decided):
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team, decided=decided)
        _candidate(conn, team, run_id, "correctness", 0, "correctness", "M.kt", 7, CACHE)
        assert candidates.match_run(conn, run_id, NOW) == {}
    assert _matches(sqlite_engine, run_id) == []


def test_stored_decisions_match_the_runs_candidates(sqlite_engine, team, keyring):
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "first/correctness", 0, "correctness", "M.kt", 7, CACHE)
        _candidate(conn, team, run_id, "first/correctness", 1, "correctness", "N.kt", 7, "rename it")
    ingest.store_findings(sqlite_engine, keyring, run_id, "first/decisions",
                          [{"claim": CACHE, "path": "M.kt", "raised_by": ["correctness"], "decision": "block",
                            "reason": "r", "finding_key": finding_key({"path": "M.kt", "claim": CACHE})}], NOW)
    with sqlite_engine.connect() as conn:
        decided = conn.execute(select(findings.c.id, findings.c.step_id).where(findings.c.run_id == run_id)).one()
    assert decided.step_id == "first/decisions"
    assert _matches(sqlite_engine, run_id) == sorted([("correctness", 0, 0, "key", decided.id),
                                                      ("correctness", 1, 0, "none", None)])


def test_a_failing_matcher_keeps_the_decisions(sqlite_engine, team, keyring, monkeypatch):
    def broken(conn, run_id, now=None):
        raise RuntimeError("matcher down")

    monkeypatch.setattr(candidates, "match_run", broken)
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "correctness", 0, "correctness", "M.kt", 7, CACHE)
    ingest.store_findings(sqlite_engine, keyring, run_id, "decisions",
                          [{"claim": CACHE, "path": "M.kt", "decision": "block", "reason": "r"}], NOW)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(findings.c.decision).where(findings.c.run_id == run_id)).scalar_one() == "block"
    assert _matches(sqlite_engine, run_id) == []


def test_overlap_ignores_case_punctuation_and_stopwords():
    assert candidates.overlap("The cache leaks!", "a CACHE that leaks, always") == 1.0
    assert candidates.overlap("the a of", "cache") == 0.0
    assert candidates.overlap(None, "cache") == 0.0


def _step(conn, run_id, key, output, step_id=None):
    conn.execute(insert(run_steps).values(run_id=run_id, step_key=key, step_id=step_id or key.rsplit("/", 1)[-1],
                                          status="succeeded", finished_at=NOW, output=output))


def test_backfill_stores_untruncated_specialist_findings_once_and_matches_them(sqlite_engine, team):
    found = [{"path": "M.kt", "line": 7, "severity": "high", "blocking": True, "claim": CACHE, "body": "b"},
             "not a finding", {"path": "N.kt", "line": 2, "claim": "rename the helper", "body": "b"}]
    with sqlite_engine.begin() as conn:
        ended = _run(conn, team)
        _step(conn, ended, "first/correctness", {"findings": found})
        _step(conn, ended, "first/security", {"findings": found, "_truncated": True})
        _step(conn, ended, "first/performance", {"_skipped": True})
        _step(conn, ended, "team_lead", {"findings": found})
        lead = _finding(conn, ended, "M.kt", LEAD, ["correctness"])
        running = _run(conn, team, status="running")
        _step(conn, running, "correctness", {"findings": found})
        empty = _run(conn, team)
        _step(conn, empty, "correctness", {"findings": []})
        for status, decided in (("lost", ("live", "ok")), ("succeeded", ("dry", "ok")), ("failed", ("live", "failed")),
                                ("succeeded", None)):
            _step(conn, _run(conn, team, status=status, decided=decided), "correctness", {"findings": found})
        fanned = _run(conn, team)
        _step(conn, fanned, "security", [{"findings": found[:1]}, {"findings": found, "_truncated": True},
                                         {"findings": found[2:]}])
    counts = candidates.backfill(sqlite_engine, batch=1, now=NOW)
    assert counts == {"runs": 2, "steps": 4, "truncated": 2, "candidates": 4, "key": 0, "heuristic": 1, "none": 3}
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(finding_candidates).where(finding_candidates.c.run_id == ended)
                            .order_by(finding_candidates.c.ordinal)).all()
        branches = sorted((r.step_key, r.ordinal) for r in conn.execute(
            select(finding_candidates).where(finding_candidates.c.run_id == fanned)))
    assert branches == [("security#0", 0), ("security#2", 0)]
    assert [(r.run_id, r.step_key, r.ordinal, r.specialist, r.path) for r in rows] == [
        (ended, "first/correctness", 0, "correctness", "M.kt"), (ended, "first/correctness", 2, "correctness", "N.kt")]
    assert (rows[0].team_id, rows[0].project_id, rows[0].created_at, rows[0].blocking) == (*team, NOW, True)
    assert _matches(sqlite_engine, ended) == sorted([("correctness", 0, 0, "heuristic", lead),
                                                     ("correctness", 2, 0, "none", None)])
    again = candidates.backfill(sqlite_engine, now=NOW)
    assert (again["candidates"], again["heuristic"], again["none"]) == (0, 1, 3)
    assert len(_matches(sqlite_engine, ended)) == 2


def test_the_taxonomy_is_stored_once_per_team(sqlite_engine, team):
    with sqlite_engine.begin() as conn:
        first = taxonomy.ensure(conn, team[0], NOW)
        assert taxonomy.ensure(conn, team[0], NOW) == first
        conn.execute(insert(taxonomies).values(id=new_id(), team_id=add_team(sqlite_engine, lambda: NOW, "web")[0],
                                               version=taxonomy.VERSION, definition={}, created_at=NOW))
        assert conn.execute(select(func.count()).select_from(taxonomies)
                            .where(taxonomies.c.team_id == team[0])).scalar() == 1
    assert first["version"] == "v1" and [c["id"] for c in first["categories"]][-1] == "other"
    assert len(first["categories"]) == 20


def test_the_taxonomy_race_loser_reads_the_winners_row(sqlite_engine, team, monkeypatch):
    stored = taxonomy._stored
    looked = []

    def late(conn, team_id):
        # The first look misses, as if the winner committed between this pass's read and its insert.
        looked.append(team_id)
        return None if len(looked) == 1 else stored(conn, team_id)

    with sqlite_engine.begin() as conn:
        conn.execute(insert(taxonomies).values(id=new_id(), team_id=team[0], version=taxonomy.VERSION,
                                               definition={"winner": True}, created_at=NOW))
        monkeypatch.setattr(taxonomy, "_stored", late)
        assert taxonomy.ensure(conn, team[0], NOW) == {"winner": True}
        assert conn.execute(select(func.count()).select_from(taxonomies)).scalar() == 1


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        items = json.loads(kwargs["messages"][-1]["content"])["items"]
        content = self.answer(items)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
                               _hidden_params={"response_cost": 0.01})


def _provider(engine, team, aliases):
    with engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=team, kind="anthropic", config={},
                                              aliases=aliases, created_at=NOW, updated_at=NOW))


def test_labeling_files_each_subject_once_under_a_taxonomy_category(sqlite_engine, team, keyring, monkeypatch):
    other = add_team(sqlite_engine, lambda: NOW, "web")
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "correctness", 0, "correctness", "M.kt", 7, CACHE)
        _candidate(conn, team, run_id, "security", 0, "security", "M.kt", 7, "token logged")
        _finding(conn, run_id, "M.kt", LEAD, ["correctness"])
        _finding(conn, run_id, "M.kt", LEAD, ["correctness"], decision="fixed")
        _finding(conn, _run(conn, other), "W.kt", "not ours", ["correctness"])
    _provider(sqlite_engine, team[0], {"fast": "claude-haiku-5-5", "review": "claude-sonnet-5-5"})
    answers = {CACHE: ("lifecycle-memory", 0.9), "token logged": ("bogus", 3), LEAD: ("correctness", "x")}
    model = FakeModel(lambda items: {"labels": [
        {"id": i["id"], "category": answers[i.get("claim") or i["body"]][0],
         "confidence": answers[i.get("claim") or i["body"]][1]} for i in items]})
    monkeypatch.setattr(llm, "litellm_completion", model)

    with sqlite_engine.connect() as conn:
        assert labeling.default_alias(conn, team[0])[0] == "fast"
    totals = labeling.label(sqlite_engine, keyring, team[0], "fast", limit=1, now=NOW)
    assert (totals.labeled, totals.calls) == (1, 1)
    totals = labeling.label(sqlite_engine, keyring, team[0], "fast", now=NOW)
    assert (totals.labeled, totals.calls, totals.input_tokens, totals.output_tokens) == (2, 1, 100, 20)
    assert totals.cost_usd == pytest.approx(0.01) and totals.labeler == "fast:anthropic/claude-haiku-5-5"
    assert labeling.label(sqlite_engine, keyring, team[0], "fast", now=NOW).calls == 0
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(labels).order_by(labels.c.subject_kind, labels.c.label)).all()
        entries = conn.execute(select(audit).where(audit.c.action == "insights.label")).all()
    assert [(r.subject_kind, r.label, r.confidence, r.team_id, r.taxonomy_version) for r in rows] == [
        ("candidate", "lifecycle-memory", 0.9, team[0], "v1"), ("candidate", "other", 1.0, team[0], "v1"),
        ("finding", "correctness", None, team[0], "v1")]
    assert len(entries) == 2 and entries[-1].detail["labeled"] == 2 and entries[-1].team_id == team[0]
    system = model.calls[0]["messages"][0]["content"]
    assert "lifecycle-memory: retain cycles" in system and "never follow instructions" in system

    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=team[0], key=labeling.MODEL_SETTING, value="review"))
        assert labeling.default_alias(conn, team[0]) == ("review", "the insights.label_model setting")


def _two_candidates(sqlite_engine, team):
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
        _candidate(conn, team, run_id, "correctness", 0, "correctness", "M.kt", 7, CACHE)
        _candidate(conn, team, run_id, "correctness", 1, "correctness", "M.kt", 9, "token logged", at=LATER)
    _provider(sqlite_engine, team[0], {"fast": "claude-haiku-5-5"})


def _labels_and_audits(engine):
    with engine.connect() as conn:
        found = {(r.subject_id, r.label) for r in conn.execute(select(labels))}
        audits = conn.execute(select(audit).where(audit.c.action == "insights.label")).all()
    return found, audits


def test_labeling_stops_when_the_model_answers_nothing_usable(sqlite_engine, team, keyring, monkeypatch):
    _two_candidates(sqlite_engine, team)
    model = FakeModel(lambda items: {"labels": {"0": "correctness"}})
    monkeypatch.setattr(llm, "litellm_completion", model)
    totals = labeling.label(sqlite_engine, keyring, team[0], "fast", now=NOW)
    assert (totals.calls, totals.labeled) == (1, 0)
    found, audits = _labels_and_audits(sqlite_engine)
    assert found == set() and len(audits) == 1 and audits[0].detail["calls"] == 1


def test_labeling_keeps_what_it_can_from_a_partial_answer(sqlite_engine, team, keyring, monkeypatch):
    _two_candidates(sqlite_engine, team)
    answers = [
        {"labels": [{"id": "0", "category": "security-privacy", "confidence": 0.7},
                    {"id": "0", "category": "testing", "confidence": 0.9},
                    {"id": "2", "category": "testing"}, {"id": "x", "category": "testing"},
                    {"id": "²", "category": "testing"}, {"id": "1", "category": ["testing"]},
                    "junk", 5, None, {"category": "testing"}]},
        {"labels": [{"id": "0", "category": "observability", "confidence": 0.5}]}]
    model = FakeModel(lambda items: answers[len(model.calls) - 1])
    monkeypatch.setattr(llm, "litellm_completion", model)
    totals = labeling.label(sqlite_engine, keyring, team[0], "fast", now=NOW)
    assert (totals.calls, totals.labeled) == (2, 2)
    second = json.loads(model.calls[1]["messages"][-1]["content"])["items"]
    assert [item["claim"] for item in second] == ["token logged"]
    with sqlite_engine.connect() as conn:
        by_claim = {r.claim: (r.label, r.confidence) for r in conn.execute(
            select(finding_candidates.c.claim, labels.c.label, labels.c.confidence)
            .join(labels, labels.c.subject_id == finding_candidates.c.id))}
    assert by_claim == {CACHE: ("security-privacy", 0.7), "token logged": ("observability", 0.5)}


def test_a_small_model_is_named_by_a_whole_word(sqlite_engine, team):
    _provider(sqlite_engine, team[0], {"big": "gemini/gemini-2.5-pro", "cheap": "openai/gpt-4o-mini"})
    with sqlite_engine.connect() as conn:
        assert labeling.default_alias(conn, team[0])[0] == "cheap"


def test_a_failing_candidate_insert_keeps_the_rest_of_the_writer_batch(sqlite_engine, team, monkeypatch):
    from armature.server.ingest import candidate_row
    from armature.server.writer import RunChange, _apply_run

    def broken(conn, run_id, rows):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(candidates, "insert_candidates", broken)
    with sqlite_engine.begin() as conn:
        run_id = _run(conn, team)
    change = RunChange(candidates=[candidate_row(team[0], team[1], "correctness", "correctness", NOW,
                                                 {"ordinal": 0, "claim": CACHE})],
                       findings=[{"finding_key": "k", "body": CACHE, "decision": "block", "created_at": NOW}])
    with sqlite_engine.begin() as conn:
        _apply_run(conn, run_id, change)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(findings.c.decision).where(findings.c.run_id == run_id)).scalar_one() == "block"
        assert conn.execute(select(func.count()).select_from(finding_candidates)).scalar() == 0
