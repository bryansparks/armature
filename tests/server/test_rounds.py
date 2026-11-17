"""Review rounds, kinds, heads, and finding lineage derived per pull request, and the backfill that re-keys findings
stored under the old fallback key."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select, text, update

from armature.server import rounds
from armature.server.db import new_id
from armature.server.db.schema import (
    findings,
    gh_pull_requests,
    gh_pushes,
    gh_repos,
    gh_reviews,
    projects,
    run_events,
    run_steps,
    runs,
    teams,
    tool_calls,
)
from armature.server.tools import Call
from armature.server.tools.marks import _approved_head
from armature.server.tools.review import finding_key
from armature.server.writer import Batch, EventWriter, RunChange, _apply_run
from tests.server.tool_support import BOT, NUMBER, REPO, clock, pem, world  # noqa: F401

SUBJECT = f"{REPO}#{NUMBER}"
T0 = datetime(2026, 11, 1, 10, tzinfo=UTC)
COLUMNS = ("head_sha", "review_round", "prev_round_run_id", "review_kind")


def _sha(n: int) -> str:
    return f"{n:040x}"


@pytest.fixture
def pr(sqlite_engine, world):
    """The pull request GitHub sync stored for the project's repository."""
    repo_id, pr_id = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=repo_id, team_id=world.team, gh_repo_id=4242, full_name="acme/app",
                                             project_id=world.project, enabled=True, created_at=T0, updated_at=T0))
        conn.execute(insert(gh_pull_requests).values(
            id=pr_id, team_id=world.team, repo_row_id=repo_id, gh_pr_id=900000 + NUMBER, number=NUMBER,
            author_gh_id=101, author_login="octo", state="open", created_at_gh=T0, gh_updated_at=T0, created_at=T0,
            updated_at=T0))
    return pr_id


def _review(engine, pr_id, team, review_id, head, login=BOT):
    with engine.begin() as conn:
        conn.execute(insert(gh_reviews).values(id=new_id(), team_id=team, pr_row_id=pr_id, gh_review_id=review_id,
                                               reviewer_gh_id=9001, reviewer_login=login, state="COMMENTED",
                                               head_sha=head, created_at=T0, updated_at=T0))


def _push(engine, pr_id, team, after, minute=0):
    with engine.begin() as conn:
        conn.execute(insert(gh_pushes).values(id=new_id(), team_id=team, pr_row_id=pr_id, before_sha="0" * 40,
                                              after_sha=after, pushed_at=T0 + timedelta(minutes=minute),
                                              source="sync", created_at=T0))


def _run(engine, world, minute, *, status="succeeded", subject=SUBJECT, project="own", steps=None, head=None):
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.team,
                                         project_id=world.project if project == "own" else project, status=status,
                                         started_at=T0 + timedelta(minutes=minute), subject=subject, head_sha=head))
        for key, output in (steps or {}).items():
            conn.execute(insert(run_steps).values(run_id=run_id, step_key=key, step_id=key, status="succeeded",
                                                  output=output))
    return run_id


def _posted(engine, world, run_id, head, *, tool="review.record-post", mode="live", target=SUBJECT.lower(),
            review_id=None):
    detail = {"head": head, "number": NUMBER, "posted": True, "approved": False,
              **({"review_id": review_id} if review_id is not None else {})}
    with engine.begin() as conn:
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=world.team, project_id=world.project,
                                               step_id="post", tool=tool, mode=mode, outcome="ok", target=target,
                                               detail=detail, created_at=T0))


def _runner(review_id=None, *, head=None, verdict="full_review", mode="review", reviewed=True):
    resolve = {"mode": mode, "reviewed": reviewed, **({"head": head} if head else {})}
    steps = {"resolve": resolve, "incremental": {"verdict": verdict}}
    if review_id is not None:
        steps["post"] = {"posted": True, "review_id": review_id}
    return steps


def _derive(engine, world, subject=SUBJECT):
    with engine.begin() as conn:
        rounds.derive(conn, subject, world.team, world.project)


def _state(engine, *ids):
    with engine.connect() as conn:
        found = {r.id: tuple(getattr(r, c) for c in COLUMNS)
                 for r in conn.execute(select(runs.c.id, *(runs.c[c] for c in COLUMNS)).where(runs.c.id.in_(ids)))}
    return [found[i] for i in ids]


def test_each_distinct_posted_head_is_a_round_and_reposting_a_head_is_an_attempt_at_its_round(
        sqlite_engine, world, pr):
    h1, h2, h3 = _sha(1), _sha(2), _sha(3)
    _review(sqlite_engine, pr, world.team, 6001, h2)
    _review(sqlite_engine, pr, world.team, 6002, h3)
    first = _run(sqlite_engine, world, 0)
    _posted(sqlite_engine, world, first, h1)
    rerun = _run(sqlite_engine, world, 1)
    _posted(sqlite_engine, world, rerun, h1, tool="github.post-review")
    runner = _run(sqlite_engine, world, 2, steps=_runner(6001))
    declined = _run(sqlite_engine, world, 3, steps=_runner(reviewed=False))
    discuss = _run(sqlite_engine, world, 4, steps=_runner(mode="discuss"))
    incremental = _run(sqlite_engine, world, 5, steps=_runner(6002, verdict="approve_incremental"))
    dry = _run(sqlite_engine, world, 6)
    _posted(sqlite_engine, world, dry, _sha(4), mode="dry")
    still_open = _run(sqlite_engine, world, 7, status="running")
    _posted(sqlite_engine, world, still_open, _sha(5))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, first, rerun, runner, declined, discuss, incremental, dry, still_open) == [
        (None, 1, None, "full"),
        (None, 1, None, "full"),
        (h2, 2, first, "full"),
        (None, None, None, "resolve_declined"),
        (None, None, None, "discuss"),
        (h3, 3, runner, "incremental"),
        (None, None, None, None),
        (None, None, None, None)]


def test_a_runners_post_counts_only_for_a_review_armature_posted_on_the_runs_own_pull_request(
        sqlite_engine, world, pr):
    other_pr = new_id()
    with sqlite_engine.begin() as conn:
        repo_id = conn.execute(select(gh_pull_requests.c.repo_row_id)).scalar()
        conn.execute(insert(gh_pull_requests).values(
            id=other_pr, team_id=world.team, repo_row_id=repo_id, gh_pr_id=900008, number=NUMBER + 1,
            author_gh_id=101, author_login="octo", state="open", created_at_gh=T0, gh_updated_at=T0, created_at=T0,
            updated_at=T0))
    _review(sqlite_engine, pr, world.team, 7001, _sha(1), login="octo")
    _review(sqlite_engine, other_pr, world.team, 7002, _sha(2))
    _review(sqlite_engine, pr, world.other, 7003, _sha(3))
    _review(sqlite_engine, pr, world.team, 7004, _sha(4))
    _review(sqlite_engine, pr, world.team, 7005, _sha(5))
    by_a_person = _run(sqlite_engine, world, 0, steps=_runner(7001))
    another_pr = _run(sqlite_engine, world, 1, steps=_runner(7002))
    another_team = _run(sqlite_engine, world, 2, steps=_runner(7003))
    webhook = _run(sqlite_engine, world, 3)
    _posted(sqlite_engine, world, webhook, _sha(4), review_id=7004)
    claims_the_webhooks = _run(sqlite_engine, world, 4, steps=_runner(7004))
    first_claim = _run(sqlite_engine, world, 5, steps=_runner(7005))
    second_claim = _run(sqlite_engine, world, 6, steps=_runner(7005))
    not_an_id = _run(sqlite_engine, world, 7, steps={"post": {"posted": True, "review_id": [7005]}})
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, by_a_person, another_pr, another_team, webhook, claims_the_webhooks, first_claim,
                  second_claim, not_an_id) == [
        (None, None, None, "full"),
        (None, None, None, "full"),
        (None, None, None, "full"),
        (None, 1, None, "full"),
        (None, None, None, "full"),
        (None, 2, webhook, "full"),
        (None, None, None, "full"),
        (None, None, None, None)]


def test_an_open_runs_claim_on_a_review_holds_back_a_later_run_that_ends_first(sqlite_engine, world, pr):
    _review(sqlite_engine, pr, world.team, 6001, _sha(1))
    _review(sqlite_engine, pr, world.team, 6002, _sha(2))
    live = _run(sqlite_engine, world, 0, status="running")
    _posted(sqlite_engine, world, live, _sha(1), review_id=6001)
    runner = _run(sqlite_engine, world, 1, steps=_runner(6001))
    earlier = _run(sqlite_engine, world, 2, status="running", steps=_runner(6002))
    later = _run(sqlite_engine, world, 3, steps=_runner(6002))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, live, runner, earlier, later) == [
        (None, None, None, None), (None, None, None, "full"), (None, None, None, None), (None, None, None, "full")]
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id.in_([live, earlier])).values(status="succeeded"))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, live, runner, earlier, later) == [
        (None, 1, None, "full"), (None, None, None, "full"), (None, 2, live, "full"), (None, None, None, "full")]


def test_a_run_that_posts_two_heads_opens_both_rounds_and_takes_its_last(sqlite_engine, world, pr):
    both = _run(sqlite_engine, world, 0)
    _posted(sqlite_engine, world, both, _sha(1))
    _posted(sqlite_engine, world, both, _sha(2))
    after = _run(sqlite_engine, world, 1)
    _posted(sqlite_engine, world, after, _sha(3))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, both, after) == [(None, 2, both, "full"), (None, 3, both, "full")]


def test_a_subject_naming_another_repository_than_its_project_derives_nothing(sqlite_engine, world, pr):
    _review(sqlite_engine, pr, world.team, 6001, _sha(1))
    elsewhere = "Evil/App#7"
    run_id = _run(sqlite_engine, world, 0, subject=elsewhere, steps=_runner(6001, head=_sha(1)))
    _push(sqlite_engine, pr, world.team, _sha(1))
    _derive(sqlite_engine, world, elsewhere)
    assert _state(sqlite_engine, run_id) == [(None, None, None, "full")]
    no_project = _run(sqlite_engine, world, 1, project=None, steps=_runner(6001))
    with sqlite_engine.begin() as conn:
        rounds.derive(conn, SUBJECT, world.team, None)
    assert _state(sqlite_engine, no_project) == [(None, None, None, None)]


def test_a_claimed_head_lands_only_when_it_was_the_pushed_head_at_the_runs_start_and_never_replaces_one(
        sqlite_engine, world, pr):
    recorded, reviewed = _sha(7), _sha(2)
    _push(sqlite_engine, pr, world.team, _sha(1), minute=0)
    _push(sqlite_engine, pr, world.team, _sha(3), minute=10)
    _review(sqlite_engine, pr, world.team, 6001, reviewed)
    confirmed = _run(sqlite_engine, world, 5, steps=_runner(head=_sha(1)))
    pushed_later = _run(sqlite_engine, world, 6, steps=_runner(head=_sha(3)))
    replaced = _run(sqlite_engine, world, 15, steps=_runner(head=_sha(1)))
    current = _run(sqlite_engine, world, 16, steps=_runner(head=_sha(3)))
    unconfirmed = _run(sqlite_engine, world, 17, steps=_runner(head=_sha(9)))
    not_a_sha = _run(sqlite_engine, world, 18, steps={"resolve": {"mode": "review", "head": "HEAD"}})
    rated = _run(sqlite_engine, world, 19, head=recorded, steps=_runner(6001, head=_sha(3)))
    _derive(sqlite_engine, world)
    assert [s[0] for s in _state(sqlite_engine, confirmed, pushed_later, replaced, current, unconfirmed, not_a_sha,
                                 rated)] == [_sha(1), None, None, _sha(3), None, None, recorded]
    assert _state(sqlite_engine, rated)[0][1] == 1


def test_a_derived_head_never_lets_an_open_run_mark_or_open_a_pull_request(sqlite_engine, world, pr):
    head = _sha(1)
    _review(sqlite_engine, pr, world.team, 6001, head)
    _push(sqlite_engine, pr, world.team, head)
    run_id = _run(sqlite_engine, world, 0, status="running", steps=_runner(6001, head=head))
    _posted(sqlite_engine, world, run_id, head)
    with sqlite_engine.begin() as conn:
        conn.execute(update(tool_calls).values(detail={"head": head, "number": NUMBER, "posted": True,
                                                       "approved": True}))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, run_id) == [(None, None, None, None)]
    call = Call(run_id=run_id, step_id="mark", connection=None, creds={}, now=T0, project_id=world.project,
                team_id=world.team, engine=sqlite_engine)
    assert _approved_head(call, REPO, NUMBER, "review.record-post") == (
        "", {"marked": False, "reason": "the run has no reviewed head"})
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status="lost"))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, run_id)[0][0] is None
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status="succeeded"))
    _derive(sqlite_engine, world)
    assert _state(sqlite_engine, run_id)[0][0] == head


def _end_in(engine, world, order, ids):
    for index in order:
        with engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == ids[index]).values(status="succeeded"))
        _derive(engine, world)
    return _state(engine, *ids)


def test_rounds_are_the_same_whichever_order_runs_end_in_and_a_second_pass_writes_nothing(
        sqlite_engine, world, pr):
    _review(sqlite_engine, pr, world.team, 6002, _sha(2))
    ids = [_run(sqlite_engine, world, 0, status="running"),
           _run(sqlite_engine, world, 1, status="running", steps=_runner(6002)),
           _run(sqlite_engine, world, 2, status="running"),
           _run(sqlite_engine, world, 3, status="running")]
    _posted(sqlite_engine, world, ids[0], _sha(1))
    _posted(sqlite_engine, world, ids[2], _sha(1))
    _posted(sqlite_engine, world, ids[3], _sha(3))
    in_order = _end_in(sqlite_engine, world, [0, 1, 2, 3], ids)
    assert in_order == [(None, 1, None, "full"), (_sha(2), 2, ids[0], "full"), (None, 1, None, "full"),
                        (None, 3, ids[1], "full")]
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).values(status="running", head_sha=None, review_round=None, prev_round_run_id=None,
                                         review_kind=None))
    assert _end_in(sqlite_engine, world, [3, 1, 2, 0], ids) == in_order

    updates = []
    event.listen(sqlite_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *_: updates.append(statement) if statement.startswith("UPDATE")
                 else None)
    _derive(sqlite_engine, world)
    assert updates == [] and _state(sqlite_engine, *ids) == in_order


def _finding(engine, world, run_id, key, path, line, body, minute=0):
    finding_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(findings).values(id=finding_id, run_id=run_id, project_id=world.project, finding_key=key,
                                             path=path, line=line, body=body,
                                             created_at=T0 + timedelta(minutes=minute)))
    return finding_id


def _lineage(engine, *ids):
    with engine.connect() as conn:
        found = dict(conn.execute(select(findings.c.id, findings.c.lineage_key).where(findings.c.id.in_(ids))).all())
    return [found[i] for i in ids]


def test_a_finding_inherits_the_lineage_of_the_nearest_match_in_an_earlier_round(sqlite_engine, world, pr):
    one, two, three = (_run(sqlite_engine, world, m) for m in (0, 1, 2))
    for run_id, n in ((one, 1), (two, 2), (three, 3)):
        _posted(sqlite_engine, world, run_id, _sha(n))
    declined = _run(sqlite_engine, world, 3, steps=_runner(reviewed=False))
    leak = _finding(sqlite_engine, world, one, "aaaaaaaaaaaa", "src/a.swift", 10,
                    "The file handle leaks when the view closes.")
    style = _finding(sqlite_engine, world, one, "bbbbbbbbbbbb", "src/b.swift", 4, "Name the constant.")
    reworded = _finding(sqlite_engine, world, two, "cccccccccccc", "src/a.swift", 14,
                        "The handle still leaks when the view closes early.")
    far = _finding(sqlite_engine, world, two, "dddddddddddd", "src/a.swift", 300,
                   "The file handle leaks when the view closes.")
    same_key = _finding(sqlite_engine, world, two, "bbbbbbbbbbbb", "src/c.swift", None, "Something else entirely.")
    again = _finding(sqlite_engine, world, three, "eeeeeeeeeeee", "src/a.swift", 16,
                     "The handle still leaks when the view closes early, as before.")
    outside = _finding(sqlite_engine, world, declined, "ffffffffffff", "src/a.swift", 10,
                       "The file handle leaks when the view closes.")
    cache = "The cache never expires its entries."
    old_cache = _finding(sqlite_engine, world, one, "hhhhhhhhhhhh", "src/d.swift", 10, cache)
    new_cache = _finding(sqlite_engine, world, two, "iiiiiiiiiiii", "src/d.swift", 30, cache)
    both_near = _finding(sqlite_engine, world, three, "jjjjjjjjjjjj", "src/d.swift", 20, cache)
    tied_first = _finding(sqlite_engine, world, two, "kkkkkkkkkkkk", "src/e.swift", 50, cache, minute=1)
    tied_second = _finding(sqlite_engine, world, two, "llllllllllll", "src/e.swift", 60, cache, minute=2)
    tie = _finding(sqlite_engine, world, three, "mmmmmmmmmmmm", "src/e.swift", 55, cache)
    _derive(sqlite_engine, world)
    assert _lineage(sqlite_engine, leak, style, reworded, far, same_key, again, outside) == [
        "aaaaaaaaaaaa", "bbbbbbbbbbbb", "aaaaaaaaaaaa", "dddddddddddd", "bbbbbbbbbbbb", "aaaaaaaaaaaa",
        "ffffffffffff"]
    # Round 2's finding is too far from round 1's to restate it, so the round 3 finding near both takes round 2's.
    assert _lineage(sqlite_engine, old_cache, new_cache, both_near) == ["hhhhhhhhhhhh", "iiiiiiiiiiii",
                                                                        "iiiiiiiiiiii"]
    assert _lineage(sqlite_engine, tied_first, tied_second, tie) == ["kkkkkkkkkkkk", "llllllllllll", "kkkkkkkkkkkk"]

    elsewhere = f"{REPO}#{NUMBER + 1}"
    other = _run(sqlite_engine, world, 4, subject=elsewhere)
    _posted(sqlite_engine, world, other, _sha(9), target=elsewhere.lower())
    copy = _finding(sqlite_engine, world, other, "gggggggggggg", "src/a.swift", 10,
                    "The file handle leaks when the view closes.")
    _derive(sqlite_engine, world, elsewhere)
    assert _lineage(sqlite_engine, copy) == ["gggggggggggg"]


def _end(engine, run_id, status="succeeded"):
    writer = EventWriter(engine, lambda: T0)
    batch = Batch()
    batch.runs[run_id] = RunChange(end=(status, T0, None))
    try:
        writer._write(batch)
    finally:
        writer._close()


def test_a_run_that_ends_derives_its_pull_requests_rounds_after_the_commit_and_a_failure_never_costs_the_write(
        sqlite_engine, world, pr, monkeypatch):
    run_id = _run(sqlite_engine, world, 0, status="running")
    _posted(sqlite_engine, world, run_id, _sha(1))
    with sqlite_engine.begin() as conn:
        _apply_run(conn, run_id, RunChange(end=("succeeded", T0, None)))
    assert _state(sqlite_engine, run_id) == [(None, None, None, None)]
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(status="running"))
    _end(sqlite_engine, run_id)
    assert _state(sqlite_engine, run_id) == [(None, 1, None, "full")]

    broken = _run(sqlite_engine, world, 1, status="running")

    def fail(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(rounds, "derive", fail)
    _end(sqlite_engine, broken, "failed")
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(runs.c.status).where(runs.c.id == broken)).scalar() == "failed"


def _legacy(path, line, body):
    return hashlib.sha256(f"{path}|{line}|{body}".encode()).hexdigest()[:64]


def test_the_backfill_rekeys_only_fallback_keys_derives_every_subject_and_a_second_pass_changes_nothing(
        sqlite_engine, world, pr):
    one = _run(sqlite_engine, world, 0)
    _posted(sqlite_engine, world, one, _sha(1))
    two = _run(sqlite_engine, world, 1)
    _posted(sqlite_engine, world, two, _sha(2))
    still_open = _run(sqlite_engine, world, 2, status="running")
    body = "The handle leaks."
    with_event = _finding(sqlite_engine, world, one, _legacy("src/a.swift", 3, body), "src/a.swift", 3, body)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(run_events).values(run_id=one, seq=1, ts=T0, kind="finding", step_id="team_lead",
                                               payload={"path": "src/a.swift", "line": 3, "body": body,
                                                        "claim": "the handle leaks"}))
    without_event = _finding(sqlite_engine, world, two, _legacy("src/a.swift", 4, "It leaks."), "src/a.swift", 4,
                             "It leaks.")
    keyed = _finding(sqlite_engine, world, two, "0123456789ab", "src/b.swift", 1, "Fine.")
    explicit = _finding(sqlite_engine, world, two, "f" * 64, "src/c.swift", 1, "Given its own key.")
    unended = _finding(sqlite_engine, world, still_open, _legacy("src/d.swift", 1, "Open."), "src/d.swift", 1,
                       "Open.")
    assert rounds.backfill(sqlite_engine) == {"subjects": 1, "rekeyed": 2}

    def keys():
        with sqlite_engine.connect() as conn:
            found = dict(conn.execute(select(findings.c.id, findings.c.finding_key)).all())
        return [found[i] for i in (with_event, without_event, keyed, explicit, unended)]

    expected = [finding_key({"path": "src/a.swift", "claim": "the handle leaks"}),
                finding_key({"path": "src/a.swift", "body": "It leaks."}), "0123456789ab", "f" * 64,
                _legacy("src/d.swift", 1, "Open.")]
    assert keys() == expected
    assert _state(sqlite_engine, one, two) == [(None, 1, None, "full"), (None, 2, one, "full")]
    assert _lineage(sqlite_engine, with_event)[0] == expected[0]
    assert rounds.backfill(sqlite_engine) == {"subjects": 1, "rekeyed": 0}
    assert keys() == expected


def test_derive_and_the_backfill_run_on_every_database_and_postgres_serializes_a_pull_request(engine):
    team, project = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="matrix", created_at=T0))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", repo=REPO, created_at=T0))
    scope = SimpleNamespace(team=team, project=project)
    repo_id, pr_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=repo_id, team_id=team, gh_repo_id=4242, full_name="acme/app",
                                             project_id=project, enabled=True, created_at=T0, updated_at=T0))
        conn.execute(insert(gh_pull_requests).values(
            id=pr_id, team_id=team, repo_row_id=repo_id, gh_pr_id=900000 + NUMBER, number=NUMBER, author_gh_id=101,
            author_login="octo", state="open", created_at_gh=T0, gh_updated_at=T0, created_at=T0, updated_at=T0))
    _push(engine, pr_id, team, _sha(2), minute=0)
    one = _run(engine, scope, 1)
    _posted(engine, scope, one, _sha(1))
    two = _run(engine, scope, 2, steps={"resolve": {"mode": "review", "reviewed": True, "head": _sha(2)}})
    _posted(engine, scope, two, _sha(2))
    body = "The handle leaks."
    first = _finding(engine, scope, one, "a" * 12, "src/a.swift", 3, body)
    second = _finding(engine, scope, two, "b" * 12, "src/a.swift", 4, body)

    with engine.connect() as conn, conn.begin():
        rounds.derive(conn, SUBJECT, team, project)
        if engine.dialect.name == "postgresql":
            held = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                                     "AND pid = pg_backend_pid()")).scalar()
            assert held == 1
            with engine.connect() as other:
                key = rounds._lock_key(team, SUBJECT.upper())
                assert other.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar() is False
                other.rollback()
    assert _state(engine, one, two) == [(None, 1, None, "full"), (_sha(2), 2, one, "full")]
    assert _lineage(engine, first, second) == ["a" * 12, "a" * 12]
    assert rounds.backfill(engine) == {"subjects": 1, "rekeyed": 0}
    assert _state(engine, one, two) == [(None, 1, None, "full"), (_sha(2), 2, one, "full")]
