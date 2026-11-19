"""Model evaluation: experiments, sampling, comparison runs queued and settled, the matcher, the blinded judge, the
scorer, recommendations, and the approve and dismiss routes."""
from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from armature.server import defs, llm, reruns, starters, worker
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    eval_arms,
    eval_experiments,
    eval_judgments,
    eval_matches,
    eval_recommendations,
    finding_candidates,
    gh_pr_files,
    gh_pr_links,
    gh_pull_requests,
    gh_repos,
    jobs,
    llm_calls,
    projects,
    providers,
    run_steps,
    runs,
    settings,
    workflows,
)
from armature.server.insights import arm_matching, arms, experiments, judge, scoring
from armature.server.tools.review import finding_key
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.live_support import add_team

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
OPUS, SONNET = "claude-opus-4-6", "claude-sonnet-4-6"
REVIEW_CAPS = ["calls:v2", "claude-code", "git", "roles:v4"]
LOCK = "the lock is never released when the parse throws on the error path"
CACHE = "the cache grows forever because nothing evicts old entries"
SEQ = itertools.count(1)


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def world(sqlite_engine, clock):
    team, project = add_team(sqlite_engine, clock, "mobile")
    other = add_team(sqlite_engine, clock, "web")
    ids = {}
    with sqlite_engine.begin() as conn:
        conn.execute(update(projects).where(projects.c.id == project).values(repo="acme/app"))
        conn.execute(insert(providers).values(id=new_id(), team_id=team, kind="anthropic", config={},
                                              aliases={"review": OPUS, "fast": "claude-haiku-4-5"}, created_at=NOW,
                                              updated_at=NOW))
        for name in ("review-pass", "code-review"):
            ids[name] = new_id()
            conn.execute(insert(workflows).values(id=ids[name], name=name, kind="review", current_version=1,
                                                  created_at=NOW, updated_at=NOW,
                                                  **defs.DefScope("project", team, project).columns()))
            defs._add_version(conn, defs.WORKFLOW, ids[name], 1, starters.body(name), "seed", None, NOW)
    return SimpleNamespace(team=team, project=project, other=other[0], other_project=other[1],
                           review_pass=ids["review-pass"], code_review=ids["code-review"])


def _runner(engine):
    """The server's own worker offers eval-arm, so arms have somewhere to run."""
    with engine.begin() as conn:
        conn.execute(insert(settings).values(key=worker.CAPABILITIES_KEY, value=[*REVIEW_CAPS, worker.EVAL_ARM]))


def _experiment(engine, world, **body):
    with engine.begin() as conn:
        return experiments.create(conn, world.team, {"specialist": "concurrency", "variable": "effort",
                                                     "variant": "high", "sample_rate": 1.0, **body},
                                  actor_id=None, now=NOW - timedelta(hours=2), ip=None, is_admin=True)


def _steps(conn, run_id, head=HEAD, panel=("correctness", "concurrency"), took=240):
    conn.execute(insert(run_steps).values(run_id=run_id, step_key="first/risk", step_id="risk", status="succeeded",
                                          parent_step="first",
                                          output={"level": "medium", "panel": list(panel), "head": head,
                                                  "reasons": [], "skipped": []}))
    conn.execute(insert(run_steps).values(run_id=run_id, step_key="first/concurrency", step_id="concurrency",
                                          status="succeeded", parent_step="first", started_at=NOW,
                                          finished_at=NOW + timedelta(seconds=took)))


def _spend(conn, team, run_id, cost, model=OPUS, at=NOW - timedelta(hours=1), step="first/concurrency",
           alias="review", effort=None, cached=False):
    conn.execute(insert(llm_calls).values(id=new_id(), team_id=team, run_id=run_id, seq=next(SEQ), step_key=step,
                                          model_id=model, cost_usd=cost, created_at=at, alias=alias,
                                          requested_effort=effort, cached=cached))


def primary(engine, world, *, head=None, number=7, panel=("correctness", "concurrency"), model=OPUS, cost=1.0,
            took=240, queued=NOW - timedelta(hours=1), steps=True):
    run_id = new_id()
    head = head or (HEAD if number == 7 else f"{number:040x}")
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, project_id=world.project,
                                         workflow_id=world.code_review, workflow_version=1, status="succeeded",
                                         started_at=queued, finished_at=queued + timedelta(minutes=30),
                                         trigger_ref={},
                                         subject=f"acme/app#{number}"))
        conn.execute(insert(jobs).values(id=new_id(), team_id=world.team, project_id=world.project,
                                         workflow_id=world.code_review, workflow_version=1, run_id=run_id,
                                         inputs={"repo": "https://github.com/acme/app.git", "slug": "acme/app",
                                                 "number": number, "ref": head, "branch": "feature",
                                                 "auto_fix": True, "round": 1},
                                         status="succeeded", attempts=1, created_at=queued,
                                         concurrency_key=f"acme/app#{number}", call_pins={"review-pass": 1},
                                         requires=REVIEW_CAPS))
        if steps:
            _steps(conn, run_id, head, panel, took)
        if model:
            _spend(conn, world.team, run_id, cost, model)
    return run_id


def _arms(engine, experiment_id, primary_run_id):
    with engine.connect() as conn:
        return {r.label: r for r in conn.execute(select(eval_arms).where(
            eval_arms.c.experiment_id == experiment_id, eval_arms.c.primary_run_id == primary_run_id))}


def _jobs_of(engine, arm_ids):
    with engine.connect() as conn:
        return {r.arm_id: r for r in conn.execute(select(jobs).where(jobs.c.arm_id.in_(arm_ids)))}


# Sampling and queueing.

def test_sampling_answers_the_same_way_every_time_at_about_the_rate():
    heads = [f"{i:040x}" for i in range(4000)]
    chosen = [h for h in heads if arms.sampled(h, "exp-1", 0.2)]
    assert 0.17 < len(chosen) / len(heads) < 0.23
    assert chosen == [h for h in heads if arms.sampled(h, "exp-1", 0.2)]
    assert chosen != [h for h in heads if arms.sampled(h, "exp-2", 0.2)]
    assert all(arms.sampled(h, "exp-1", 1.0) for h in heads[:50])


def test_a_sampled_review_queues_a_variant_and_a_control_at_its_head(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    assert experiment.baseline == {"alias": "review", "model": OPUS, "effort": None}
    run_id = primary(sqlite_engine, world)
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 2, "skipped": 0}
    found = _arms(sqlite_engine, experiment.id, run_id)
    stand_ins = {"rate": {"output": {"level": "medium", "reasons": [], "commit": HEAD}},
                 "team_lead": {"output": experiments.NO_LEAD}}
    assert {label: (a.status, a.head_sha, a.overrides) for label, a in found.items()} == {
        "control": ("queued", HEAD, {"concurrency": {"model": OPUS}, **stand_ins}),
        "variant": ("queued", HEAD, {"concurrency": {"model": OPUS, "effort": "high"}, **stand_ins})}
    queued = _jobs_of(sqlite_engine, [a.id for a in found.values()])
    for arm in found.values():
        job = queued[arm.id]
        assert job.concurrency_key == f"acme/app#7#arm:{arm.id}" and job.status == "queued"
        assert job.inputs == {"repo": "https://github.com/acme/app.git", "slug": "acme/app", "number": 7,
                              "ref": HEAD, "branch": "", "auto_fix": False, "round": 1}
        assert job.workflow_version == 1 and job.call_pins == {"review-pass": 1}
        assert job.requires == sorted([*REVIEW_CAPS, worker.EVAL_ARM])
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 0, "skipped": 0}


def test_unsampled_unreviewed_or_older_reviews_get_no_arms(sqlite_engine, world):
    _runner(sqlite_engine)
    rare = _experiment(sqlite_engine, world, sample_rate=0.01)
    unsampled = next(h for h in (f"{i:040x}" for i in range(100)) if not arms.sampled(h, rare.id, 0.01))
    reviewed = primary(sqlite_engine, world, head=unsampled)
    every = _experiment(sqlite_engine, world, variable="model", variant=SONNET)
    older = primary(sqlite_engine, world, number=8, queued=NOW - timedelta(hours=3))
    noop = primary(sqlite_engine, world, number=9, steps=False)
    assert experiments.queue_arms(sqlite_engine, rare, NOW) == {"queued": 0, "skipped": 0}
    assert experiments.queue_arms(sqlite_engine, every, NOW) == {"queued": 2, "skipped": 0}
    assert set(_arms(sqlite_engine, every.id, reviewed)) == {"variant", "control"}
    assert _arms(sqlite_engine, every.id, older) == _arms(sqlite_engine, every.id, noop) == {}


def test_a_specialist_off_the_panel_skips_both_arms_saying_why(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world, panel=("correctness",))
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 0, "skipped": 2}
    found = _arms(sqlite_engine, experiment.id, run_id)
    assert {a.status for a in found.values()} == {"skipped"}
    assert {a.reason for a in found.values()} == {"concurrency wasn't on this review's panel."}
    assert _jobs_of(sqlite_engine, [a.id for a in found.values()]) == {}


def test_without_a_runner_or_environment_type_the_arms_skip_and_the_review_is_untouched(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 0, "skipped": 2}
    found = _arms(sqlite_engine, experiment.id, run_id)
    assert {a.reason for a in found.values()} == {experiments.NO_ENVIRONMENT}
    with sqlite_engine.connect() as conn:
        run = conn.execute(select(runs).where(runs.c.id == run_id)).one()
        job = conn.execute(select(jobs).where(jobs.c.run_id == run_id)).one()
        assert conn.execute(select(jobs).where(jobs.c.arm_id.is_not(None))).all() == []
    assert (run.status, job.status, job.cancel_requested_at) == ("succeeded", "succeeded", None)


def _done_arm(engine, world, experiment, primary_run_id, label, *, cost=0.0, at=NOW - timedelta(hours=1),
              status="done"):
    run_id = new_id()
    arm_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, project_id=world.project, status="succeeded",
                                         started_at=NOW, finished_at=NOW, trigger_ref={"arm_id": arm_id}))
        head = conn.execute(select(jobs.c.inputs).where(jobs.c.run_id == primary_run_id)).scalar()["ref"]
        conn.execute(insert(eval_arms).values(id=arm_id, team_id=world.team, experiment_id=experiment.id,
                                              primary_run_id=primary_run_id, arm_run_id=run_id, head_sha=head,
                                              label=label, status=status, created_at=NOW))
        _steps(conn, run_id)
        if cost:
            _spend(conn, world.team, run_id, cost, at=at)
    return run_id


def test_the_daily_budget_counts_arm_calls_and_the_judge_and_then_skips_arms(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world, daily_budget_usd=2)
    earlier = primary(sqlite_engine, world, number=5, queued=NOW - timedelta(days=3))
    _done_arm(sqlite_engine, world, experiment, earlier, "variant", cost=1.25)
    _done_arm(sqlite_engine, world, experiment, earlier, "control", cost=9.0, at=NOW - timedelta(days=1))
    with sqlite_engine.begin() as conn:
        assert arms.spent_today(conn, experiment.id, NOW) == pytest.approx(1.25)
        candidate = _candidate(conn, world, earlier, 0, "A.kt", 3, LOCK)
        conn.execute(insert(eval_judgments).values(id=new_id(), candidate_id=candidate, team_id=world.team,
                                                   experiment_id=experiment.id, valid=True, judge="t",
                                                   cost_usd=0.75, created_at=NOW))
        assert arms.spent_today(conn, experiment.id, NOW) == pytest.approx(2.0)
    run_id = primary(sqlite_engine, world)
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 0, "skipped": 2}
    assert {a.reason for a in _arms(sqlite_engine, experiment.id, run_id).values()} == {
        "Today's comparison budget of $2 is spent."}


def test_arms_in_flight_count_at_their_expected_cost_so_the_budget_holds(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world, daily_budget_usd=4, max_concurrent_arms=20)
    first = primary(sqlite_engine, world, number=7, queued=NOW - timedelta(minutes=50))
    second = primary(sqlite_engine, world, number=8, queued=NOW - timedelta(minutes=40))
    third = primary(sqlite_engine, world, number=9, queued=NOW - timedelta(minutes=30))
    with sqlite_engine.connect() as conn:
        assert arms.expected_cost(conn, experiment) == Decimal("1.50")
    # No spend is recorded yet. The first pair holds $3 of $4 at $1.50 each, so another pair would pass the budget.
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 2, "skipped": 4}
    assert {a.status for a in _arms(sqlite_engine, experiment.id, first).values()} == {"queued"}
    for later in (second, third):
        assert {a.reason for a in _arms(sqlite_engine, experiment.id, later).values()} == {
            "Today's comparison budget of $4 doesn't cover two more comparison runs on top of those already running."}
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key=arms.ESTIMATE_SETTING, value=0.5))
        assert arms.expected_cost(conn, experiment) == Decimal("0.5")
        assert arms.committed(conn, experiment, NOW, 2) == Decimal("2.0")
        conn.execute(update(eval_arms).where(eval_arms.c.primary_run_id == first, eval_arms.c.label == "variant")
                     .values(status="done", cost_usd=0.25))
        assert arms.expected_cost(conn, experiment) == Decimal("0.25")
        assert arms.committed(conn, experiment, NOW) == Decimal("0.25")


def test_arms_wait_for_the_experiments_cap_and_start_once_earlier_arms_end(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    first = primary(sqlite_engine, world, number=7, queued=NOW - timedelta(minutes=50))
    second = primary(sqlite_engine, world, number=8, queued=NOW - timedelta(minutes=40))
    assert experiments.queue_arms(sqlite_engine, experiment, NOW)["queued"] == 2
    assert _arms(sqlite_engine, experiment.id, second) == {}
    held = _arms(sqlite_engine, experiment.id, first)
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.arm_id.in_([a.id for a in held.values()]))
                     .values(status="failed", error="No environment started for this job."))
    assert experiments.settle(sqlite_engine, NOW) == {experiment.id}
    assert {a.status for a in _arms(sqlite_engine, experiment.id, first).values()} == {"skipped"}
    assert experiments.queue_arms(sqlite_engine, experiment, NOW)["queued"] == 2
    assert {a.status for a in _arms(sqlite_engine, experiment.id, second).values()} == {"queued"}


def test_settling_follows_each_arms_job_and_run(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    experiments.queue_arms(sqlite_engine, experiment, NOW)
    found = _arms(sqlite_engine, experiment.id, run_id)
    variant_run = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=variant_run, team_id=world.team, status="succeeded", started_at=NOW,
                                         finished_at=NOW + timedelta(seconds=90), cost_usd=0.5,
                                         trigger_ref={"arm_id": found["variant"].id}))
        conn.execute(update(jobs).where(jobs.c.arm_id == found["variant"].id).values(status="running",
                                                                                    run_id=variant_run))
    assert experiments.settle(sqlite_engine, NOW) == set()
    assert _arms(sqlite_engine, experiment.id, run_id)["variant"].status == "running"
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.arm_id == found["variant"].id).values(status="succeeded"))
        conn.execute(update(jobs).where(jobs.c.arm_id == found["control"].id).values(
            status="failed", error="The workflow no longer exists."))
    experiments.settle(sqlite_engine, NOW)
    after = _arms(sqlite_engine, experiment.id, run_id)
    assert (after["variant"].status, float(after["variant"].cost_usd), after["variant"].latency_s,
            after["variant"].reason) == ("done", 0.5, 90.0, None)
    assert (after["control"].status, after["control"].reason) == ("skipped", "The workflow no longer exists.")


def test_a_comparison_run_cant_be_rerun(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    experiments.queue_arms(sqlite_engine, experiment, NOW)
    arm = _arms(sqlite_engine, experiment.id, run_id)["variant"]
    arm_run = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=arm_run, team_id=world.team, status="succeeded", started_at=NOW,
                                         finished_at=NOW, trigger_ref={"arm_id": arm.id}))
        conn.execute(update(jobs).where(jobs.c.arm_id == arm.id).values(status="succeeded", run_id=arm_run))
        with pytest.raises(reruns.RerunError) as refused:
            reruns.check(conn, arm_run)
    assert refused.value.status == 409 and "comparison run" in refused.value.message


def test_a_comparison_runs_candidates_carry_its_arm(sqlite_engine, world):
    from armature.server.insights import candidates
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    arm_run = _done_arm(sqlite_engine, world, experiment, run_id, "variant")
    row = {"team_id": world.team, "project_id": world.project, "step_key": "first/concurrency",
           "specialist": "concurrency", "ordinal": 0, "path": "A.kt", "claim": LOCK, "created_at": NOW}
    with sqlite_engine.begin() as conn:
        assert candidates.insert_candidates(conn, arm_run, [row]) == 1
        assert candidates.insert_candidates(conn, run_id, [row]) == 1
        tagged = dict(conn.execute(select(finding_candidates.c.run_id, finding_candidates.c.arm_id)).all())
        arm_id = conn.execute(select(eval_arms.c.id).where(eval_arms.c.arm_run_id == arm_run)).scalar()
    assert tagged == {arm_run: arm_id, run_id: None}


# Matching.

def _candidate(conn, world, run_id, ordinal, path, line, claim, step="first/concurrency", arm_id=None):
    candidate_id = new_id()
    conn.execute(insert(finding_candidates).values(
        id=candidate_id, team_id=world.team, project_id=world.project, run_id=run_id, step_key=step,
        specialist=step.rpartition("/")[2], ordinal=ordinal, finding_key=finding_key({"path": path, "claim": claim}),
        path=path, line=line, claim=claim, arm_id=arm_id, created_at=NOW))
    return candidate_id


def test_the_matcher_pairs_one_to_one_best_first_and_keeps_the_rest_unmatched(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    variant = _done_arm(sqlite_engine, world, experiment, run_id, "variant")
    _done_arm(sqlite_engine, world, experiment, run_id, "control")
    with sqlite_engine.begin() as conn:
        p_lock = _candidate(conn, world, run_id, 0, "A.kt", 10, LOCK)
        p_cache = _candidate(conn, world, run_id, 1, "B.kt", 5, CACHE)
        _candidate(conn, world, run_id, 0, "A.kt", 10, LOCK, step="first/correctness")
        close = _candidate(conn, world, variant, 0, "A.kt", 12, "the lock is never released when parse throws")
        closer = _candidate(conn, world, variant, 1, "A.kt", 14, "lock never released when the parse throws on error")
        keyed = _candidate(conn, world, variant, 2, "B.kt", 90, CACHE)
        far = _candidate(conn, world, variant, 3, "A.kt", 60, LOCK + " again")
    with sqlite_engine.connect() as conn:
        assert arm_matching.pending(conn, experiment) == [run_id]
    with sqlite_engine.begin() as conn:
        assert arm_matching.match(conn, experiment, run_id) == 4
    with sqlite_engine.connect() as conn:
        rows = {r.left_candidate_id: (r.right_candidate_id, r.method) for r in conn.execute(select(eval_matches))}
        assert arm_matching.pending(conn, experiment) == []
    assert rows[keyed] == (p_cache, "key")
    assert rows[far] == (None, "none")
    assert {rows[close][0], rows[closer][0]} == {p_lock, None}
    with sqlite_engine.connect() as conn:
        scores = {r.left_candidate_id: r.score for r in conn.execute(select(eval_matches))}
    winner = close if rows[close][0] else closer
    assert scores[winner] >= max(s for k, s in scores.items() if k in (close, closer) and s is not None)


# The judge.

class FakeJudge:
    def __init__(self, valid=lambda item: True):
        self.valid = valid
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        items = json.loads(kwargs["messages"][-1]["content"])["items"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
            {"judgments": [{"id": i["id"], "valid": self.valid(i), "confidence": 0.8} for i in items]})))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20), _hidden_params={"response_cost": 0.03})


def _pair(engine, world, experiment, number=7, primary_found=((LOCK, "A.kt"), (CACHE, "B.kt")),
          variant_found=((LOCK, "A.kt"),), control_found=((LOCK, "A.kt"),), costs=(1.0, 0.6, 1.0)):
    run_id = primary(engine, world, number=number, head=f"{number:040x}", cost=costs[0])
    found = {"primary": run_id}
    for label, cost in (("variant", costs[1]), ("control", costs[2])):
        found[label] = _done_arm(engine, world, experiment, run_id, label, cost=cost)
    with engine.begin() as conn:
        for label, items in (("primary", primary_found), ("variant", variant_found), ("control", control_found)):
            for ordinal, (claim, path) in enumerate(items):
                _candidate(conn, world, found[label], ordinal, path, 4, claim)
    return found


def test_the_judge_sees_every_finding_shuffled_with_no_run_or_setting_named(sqlite_engine, keyring, world,
                                                                             monkeypatch):
    experiment = _experiment(sqlite_engine, world)
    found = _pair(sqlite_engine, world, experiment, control_found=((LOCK, "A.kt"), ("a nit", "C.kt")))
    model = FakeJudge(lambda item: item["claim"] != "a nit")
    monkeypatch.setattr(llm, "litellm_completion", model)
    with sqlite_engine.connect() as conn:
        assert judge.judge_alias(conn, world.team)[0] == "review"
    assert judge.judge_experiment(sqlite_engine, keyring, experiment, NOW) == 5
    [call] = model.calls
    sent = json.dumps(call["messages"])
    with sqlite_engine.connect() as conn:
        arm_ids = [a.id for a in conn.execute(select(eval_arms))]
    for hidden in ("variant", "control", "primary", "arm", *found.values(), *arm_ids, experiment.id):
        assert hidden not in sent
    items = json.loads(call["messages"][-1]["content"])["items"]
    assert [i["id"] for i in items] == ["0", "1", "2", "3", "4"]
    with sqlite_engine.connect() as conn:
        stored = [c.claim for run in (found["primary"], found["variant"], found["control"])
                  for c in arm_matching.candidates_of(conn, run, "concurrency")]
    sent_claims = [i["claim"] for i in items]
    assert sorted(sent_claims) == sorted(stored) and sent_claims != stored
    assert set(items[0]) <= {"id", "path", "line", "severity", "claim", "body", "suggestion"}
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(eval_judgments)).all()
        logged = conn.execute(select(audit).where(audit.c.action == "insights.judge")).one()
    assert sorted(r.valid for r in rows) == [False, True, True, True, True]
    assert sum(float(r.cost_usd) for r in rows) == pytest.approx(0.03) and rows[0].judge.startswith("review:")
    assert logged.detail["judged"] == 5 and logged.target_id == experiment.id
    assert judge.judge_experiment(sqlite_engine, keyring, experiment, NOW) == 0 and len(model.calls) == 1


def test_the_judge_waits_while_the_budget_is_spent(sqlite_engine, keyring, world, monkeypatch):
    experiment = _experiment(sqlite_engine, world, daily_budget_usd=1)
    _pair(sqlite_engine, world, experiment)
    model = FakeJudge()
    monkeypatch.setattr(llm, "litellm_completion", model)
    assert judge.judge_experiment(sqlite_engine, keyring, experiment, NOW) == 0 and model.calls == []


def test_a_later_fix_of_the_flagged_file_overrides_the_judge(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    found = _pair(sqlite_engine, world, experiment)
    repo = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=repo, team_id=world.team, gh_repo_id=1, full_name="Acme/App",
                                             enabled=True, created_at=NOW, updated_at=NOW))
        pr_ids = {}
        for number, created in ((7, NOW - timedelta(days=1)), (8, NOW + timedelta(days=1)),
                                (9, NOW + timedelta(days=1)), (10, NOW - timedelta(hours=2))):
            pr_ids[number] = new_id()
            conn.execute(insert(gh_pull_requests).values(
                id=pr_ids[number], team_id=world.team, repo_row_id=repo, gh_pr_id=number, number=number,
                author_gh_id=1, author_login="ana", state="merged", created_at_gh=created, gh_updated_at=created,
                created_at=NOW, updated_at=NOW))
        conn.execute(insert(gh_pr_links).values(id=new_id(), team_id=world.team, from_pr_row_id=pr_ids[8],
                                                to_pr_row_id=pr_ids[7], kind="fix", confidence=0.9,
                                                detector_version="1", created_at=NOW))
        conn.execute(insert(gh_pr_files).values(id=new_id(), team_id=world.team, repo_row_id=repo,
                                                pr_row_id=pr_ids[8], path="A.kt", path_key="k", created_at=NOW))
        # Unknown files and a fix opened before the review prove nothing about B.kt.
        for fixer in (9, 10):
            conn.execute(insert(gh_pr_links).values(id=new_id(), team_id=world.team, from_pr_row_id=pr_ids[fixer],
                                                    to_pr_row_id=pr_ids[7], kind="fix", confidence=0.9,
                                                    detector_version="1", created_at=NOW))
        conn.execute(insert(gh_pr_files).values(id=new_id(), team_id=world.team, repo_row_id=repo,
                                                pr_row_id=pr_ids[10], path="B.kt", path_key="b", created_at=NOW))
        for c in conn.execute(select(finding_candidates)).all():
            conn.execute(insert(eval_judgments).values(id=new_id(), candidate_id=c.id, team_id=world.team,
                                                       experiment_id=experiment.id, valid=False, judge="review:x",
                                                       created_at=NOW))
        assert judge.apply_evidence(conn, experiment) == 3
        rows = {(r.path, r.run_id): (r.valid, r.judge) for r in conn.execute(
            select(finding_candidates.c.path, finding_candidates.c.run_id, eval_judgments.c.valid,
                   eval_judgments.c.judge).join(eval_judgments,
                                                eval_judgments.c.candidate_id == finding_candidates.c.id))}
    assert rows[("A.kt", found["variant"])] == (True, judge.EVIDENCE)
    assert rows[("A.kt", found["primary"])] == (True, judge.EVIDENCE)
    assert rows[("B.kt", found["primary"])] == (False, "review:x")


# Scoring and recommendations.

def _judge_all(engine, experiment, valid=lambda c: c.claim != "a nit"):
    with engine.begin() as conn:
        judged = select(eval_judgments.c.candidate_id)
        for c in conn.execute(select(finding_candidates).where(finding_candidates.c.id.not_in(judged))).all():
            conn.execute(insert(eval_judgments).values(id=new_id(), candidate_id=c.id, team_id=c.team_id,
                                                       experiment_id=experiment.id, valid=valid(c), judge="t",
                                                       created_at=NOW))


def _scored(engine, world, experiment, numbers, **kwargs):
    for number in numbers:
        found = _pair(engine, world, experiment, number=number, **kwargs)
        with engine.begin() as conn:
            arm_matching.match(conn, experiment, found["primary"])
    _judge_all(engine, experiment)


def test_the_scorer_measures_each_run_and_the_effect_against_the_control(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    _scored(sqlite_engine, world, experiment, [1], primary_found=((LOCK, "A.kt"), ("a nit", "N.kt")),
            variant_found=((LOCK, "A.kt"), (CACHE, "B.kt")), control_found=((LOCK, "A.kt"),))
    with sqlite_engine.connect() as conn:
        computed = scoring.compute(conn, experiment)
    labels, test = computed["labels"], computed["test"]
    assert computed["prs"] == 1 and computed["real"] == 2
    pick = ("recall", "precision", "unique_valid", "cost_usd", "latency_s")
    assert {k: labels["primary"][k] for k in pick} == {"recall": 0.5, "precision": 0.5, "unique_valid": 0,
                                                       "cost_usd": 1.0, "latency_s": 240.0}
    assert {k: labels["variant"][k] for k in pick[:4]} == {"recall": 1.0, "precision": 1.0, "unique_valid": 1,
                                                           "cost_usd": 0.6}
    assert {m: test["effect"][m]["mean"] for m in scoring.MEASURES} == pytest.approx(
        {"recall": 0.5, "precision": 0.0, "unique_valid": 1, "cost_usd": -0.4})
    assert {m: test["shift"][m]["mean"] for m in scoring.MEASURES} == pytest.approx(
        {"recall": 0.0, "precision": 0.5, "unique_valid": 0, "cost_usd": 0.0})
    # One pull request has no standard error, so nothing counts yet.
    assert not any(test["effect"][m]["significant"] for m in scoring.MEASURES)


def test_an_issue_both_comparison_runs_found_and_the_review_missed_counts_once(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    found = _pair(sqlite_engine, world, experiment, primary_found=((LOCK, "A.kt"),),
                  variant_found=((LOCK, "A.kt"), (CACHE, "B.kt")), control_found=((CACHE, "B.kt"),))
    with sqlite_engine.begin() as conn:
        arm_matching.match(conn, experiment, found["primary"])
        rows = {r.left_candidate_id: (r.right_candidate_id, r.method) for r in conn.execute(select(eval_matches))}
        cache = {c.run_id: c.id for c in conn.execute(select(finding_candidates).where(
            finding_candidates.c.claim == CACHE))}
    assert rows[cache[found["variant"]]] == (cache[found["control"]], "key")
    assert rows[cache[found["control"]]] == (None, "none")
    _judge_all(sqlite_engine, experiment)
    with sqlite_engine.connect() as conn:
        computed = scoring.compute(conn, experiment)
    assert computed["real"] == 2 and computed["labels"]["variant"]["unique_valid"] == 0
    assert computed["labels"]["control"]["found"] == 1 and computed["labels"]["variant"]["found"] == 2


def test_a_review_with_an_unjudged_finding_waits(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    _pair(sqlite_engine, world, experiment)
    with sqlite_engine.connect() as conn:
        assert scoring.compute(conn, experiment)["prs"] == 0


def _review_per(**labels):
    base = {"recall": 0.5, "precision": 1.0, "unique_valid": 0, "cost_usd": 1.0}
    return {"per": {label: {**base, **labels.get(label, {})} for label in ("primary", "variant", "control")}}


def test_a_difference_counts_only_past_twice_its_standard_error():
    def pairs(a, b):
        return [_review_per(variant={"cost_usd": a}, control={"cost_usd": 0.0}),
                _review_per(variant={"cost_usd": b}, control={"cost_usd": 0.0})]

    exact = scoring.difference(pairs(3.0, 1.0), "cost_usd", "variant", "control")
    assert (exact["mean"], exact["se"], exact["significant"]) == (2.0, 1.0, False)
    assert scoring.difference(pairs(3.0, 1.1), "cost_usd", "variant", "control")["significant"] is True
    steady = scoring.difference([_review_per(variant={"cost_usd": 0.6})] * 3, "cost_usd", "variant", "control")
    assert steady["significant"] is True and steady["se"] == 0
    one = scoring.difference([_review_per(variant={"cost_usd": 0.6})], "cost_usd", "variant", "control")
    assert (one["n"], one["se"], one["significant"]) == (1, None, False)
    missing = scoring.difference([_review_per(variant={"precision": None})], "precision", "variant", "control")
    assert missing == {"mean": None, "se": None, "n": 0, "significant": False}


def _labels(prs=30, variant_cost=0.6):
    base = {"prs": prs, "recall": 0.8, "precision": 0.7, "unique_valid": 2, "cost_usd": 1.0, "latency_s": 100.0,
            "found": 8}
    return {"primary": dict(base), "control": dict(base), "variant": {**base, "cost_usd": variant_cost}}


def _test(**effects):
    return {"effect": {m: {"mean": effects.get(m, (0.0, False))[0], "se": 0.01, "n": 30,
                           "significant": effects.get(m, (0.0, False))[1]} for m in scoring.MEASURES},
            "shift": {}}


def test_a_result_follows_only_effects_that_count_and_the_minimum_pull_requests():
    experiment = SimpleNamespace(min_prs=30)
    cheaper = _test(cost_usd=(-0.4, True))
    assert scoring.verdict(experiment, _labels(), cheaper) == {
        "kind": "cheaper", "words": "Found as many real issues, within normal variation, at 60% of the cost",
        "prs": 30, "cost_share": 0.6}
    assert scoring.verdict(experiment, _labels(), _test(cost_usd=(-0.4, False)))["words"] == scoring.WITHIN
    assert scoring.verdict(SimpleNamespace(min_prs=31), _labels(), cheaper)["words"] == scoring.EARLY
    assert scoring.verdict(experiment, _labels(), _test(recall=(0.1, True)))["kind"] == "better"
    assert scoring.verdict(experiment, _labels(), _test(recall=(-0.1, True), cost_usd=(-0.4, True)))["kind"] == "worse"
    worse_precision = _test(cost_usd=(-0.4, True), precision=(-0.2, True))
    assert scoring.verdict(experiment, _labels(), worse_precision)["kind"] == "within"
    assert scoring.verdict(experiment, _labels(), _test(cost_usd=(-0.4, True), precision=(0.2, True)))["kind"] == (
        "cheaper")
    assert scoring.verdict(experiment, None)["words"] == scoring.EARLY


def test_the_weekly_job_recommends_once_and_never_again_after_a_dismissal(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world, min_prs=5)
    _scored(sqlite_engine, world, experiment, range(1, 5))
    assert scoring.recommend(sqlite_engine, world.team, NOW) == 0
    _scored(sqlite_engine, world, experiment, [5])
    assert scoring.recommend(sqlite_engine, world.team, NOW) == 1
    assert scoring.recommend(sqlite_engine, world.team, NOW) == 0
    with sqlite_engine.connect() as conn:
        [row] = conn.execute(select(eval_recommendations)).all()
    assert row.sentence == ("Over 5 pull requests, concurrency at high effort found as much as the default effort "
                            "found at 60% of the cost.")
    assert row.change == {"specialist": "concurrency", "effort": "high"} and row.state == "waiting"
    assert row.evidence["prs"] == 5 and row.evidence["result"] == "cheaper"
    assert row.evidence["effect"]["cost_usd"]["significant"] is True and "environment_shift" in row.evidence
    with sqlite_engine.begin() as conn:
        conn.execute(update(eval_recommendations).values(state="dismissed"))
    assert scoring.recommend(sqlite_engine, world.team, NOW) == 0


def test_a_cheaper_model_reads_as_much_as_in_product_names(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world, variable="model", variant=SONNET, min_prs=5)
    _scored(sqlite_engine, world, experiment, range(1, 6))
    assert scoring.recommend(sqlite_engine, world.team, NOW) == 1
    with sqlite_engine.connect() as conn:
        sentence = conn.execute(select(eval_recommendations.c.sentence)).scalar()
    assert sentence == ("Over 5 pull requests, concurrency on Claude Sonnet 4.6 found as much as Claude Opus 4.6 found "
                        "at 60% of the cost.")


def test_a_better_variant_says_how_many_more_real_issues_it_found(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world, variable="model", variant=SONNET, min_prs=5)
    _scored(sqlite_engine, world, experiment, range(1, 6),
            variant_found=((LOCK, "A.kt"), (CACHE, "B.kt"), ("race", "R.kt")), costs=(1.0, 1.5, 1.0))
    with sqlite_engine.connect() as conn:
        computed = scoring.compute(conn, experiment)
    assert scoring.verdict(experiment, computed["labels"], computed["test"])["kind"] == "better"
    assert scoring.sentence(experiment, computed, "better") == (
        "Over 5 pull requests, concurrency on Claude Sonnet 4.6 found 10 more real issues than Claude Opus 4.6 at "
        "150% of the cost.")


def test_model_names_match_the_models_screen():
    cases = json.loads((Path(__file__).parent / "model_names.json").read_text())
    assert len(cases) >= 6
    for model_id, name in cases:
        assert scoring.model_name(model_id) == name, model_id


# Routes.

@pytest.fixture
def users(sqlite_engine, keyring, clock, world):
    found = {}
    for name, team, role in (("lead", world.team, "team_lead"), ("member", world.team, "contributor"),
                             ("outsider", world.other, "team_lead")):
        found[name] = add_user(sqlite_engine, keyring, clock, name)[0]
        add_membership(sqlite_engine, clock, found[name], "team", team, role)
    found["admin"] = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)[0]
    return found


def _client(engine, keyring, clock, user_id, step_up=True):
    client = make_client(engine, keyring, clock)
    headers = mutation(sign_in_as(client, engine, keyring, clock, user_id, step_up=step_up))
    return SimpleNamespace(client=client, headers=headers)


@pytest.fixture(autouse=True)
def fresh_answers():
    from armature.server.app import routes_insights
    routes_insights.answers.clear()
    yield
    routes_insights.answers.clear()


def _post(who, world, route, body=None):
    return who.client.post(f"/api/teams/{world.team}/insights/models{route}", json=body, headers=who.headers)


def _recommendation(engine, world, experiment, change):
    rec_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(eval_recommendations).values(id=rec_id, team_id=world.team, experiment_id=experiment.id,
                                                         specialist="concurrency", change=change, sentence="s",
                                                         evidence={}, state="waiting", created_at=NOW))
    return rec_id


def _body(engine, world, workflow_id=None):
    with engine.connect() as conn:
        row = conn.execute(select(workflows).where(workflows.c.id == (workflow_id or world.review_pass))).one()
        return row.current_version, defs.body_of(conn, defs.WORKFLOW, row.id, row.current_version)


def _second_project(engine, world):
    """Another project of the team with its own review-pass, as migrate_ops makes one per project."""
    project, workflow = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(projects).values(id=project, team_id=world.team, name="ios", repo="acme/ios",
                                             created_at=NOW))
        conn.execute(insert(workflows).values(id=workflow, name="review-pass", kind="review", current_version=1,
                                              created_at=NOW, updated_at=NOW,
                                              **defs.DefScope("project", world.team, project).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, starters.body("review-pass"), "seed", None, NOW)
    return project, workflow


def test_approving_an_effort_change_saves_a_new_review_pass_version_and_audits_it(sqlite_engine, keyring, clock,
                                                                                   world, users):
    experiment = _experiment(sqlite_engine, world)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "effort": "high"})
    stale = _client(sqlite_engine, keyring, clock, users["lead"], step_up=False)
    refused = _post(stale, world, f"/recommendations/{rec_id}/approve")
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"
    member = _client(sqlite_engine, keyring, clock, users["member"])
    assert _post(member, world, f"/recommendations/{rec_id}/approve").status_code == 403
    _, ios = _second_project(sqlite_engine, world)
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    response = _post(lead, world, f"/recommendations/{rec_id}/approve",
                     {"change": {"specialist": "concurrency", "effort": "max"}, "effort": "max"})
    assert response.status_code == 200, response.text
    applied = {a["workflow_id"]: a for a in response.json()["applied"]}
    assert set(applied) == {world.review_pass, ios}
    for found in applied.values():
        assert found["version"] == 2 and found["before"] == {"model": "review", "effort": None}
        assert found["after"] == {"model": "review", "effort": "high"}
    assert experiments.agent_stage(_body(sqlite_engine, world, ios)[1], "concurrency")["effort"] == "high"
    version, body = _body(sqlite_engine, world)
    assert version == 2 and experiments.agent_stage(body, "concurrency")["effort"] == "high"
    assert experiments.agent_stage(body, "correctness").get("effort") is None
    assert body.replace("      effort: high\n", "", 1) == starters.body("review-pass")
    with sqlite_engine.connect() as conn:
        rec = conn.execute(select(eval_recommendations)).one()
        logged = conn.execute(select(audit).where(audit.c.action == "insights.recommendation.approve")).one()
        status = conn.execute(select(eval_experiments.c.status)).scalar()
    assert (rec.state, rec.decided_by) == ("applied", users["lead"]) and status == "finished"
    assert sorted(rec.applied_version, key=lambda v: v["workflow_id"]) == sorted(
        [{"workflow_id": world.review_pass, "version": 2}, {"workflow_id": ios, "version": 2}],
        key=lambda v: v["workflow_id"])
    assert logged.actor_id == users["lead"] and len(logged.detail["applied"]) == 2
    assert logged.detail["applied"][0]["before"] == {"model": "review", "effort": None}
    with sqlite_engine.connect() as conn:
        saved = conn.execute(select(audit).where(audit.c.action == "workflow.version")).all()
    assert {a.actor_id for a in saved} == {users["lead"]} and len(saved) == 2
    assert _post(lead, world, f"/recommendations/{rec_id}/approve").status_code == 409


def test_a_team_lead_without_write_on_one_review_pass_applies_nothing(sqlite_engine, keyring, clock, world, users,
                                                                    monkeypatch):
    experiment = _experiment(sqlite_engine, world)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "effort": "high"})
    ios_project, ios = _second_project(sqlite_engine, world)
    real = defs.can_write
    monkeypatch.setattr(defs, "can_write", lambda access, scope: scope.project_id != ios_project
                        and real(access, scope))
    response = _post(_client(sqlite_engine, keyring, clock, users["lead"]), world,
                     f"/recommendations/{rec_id}/approve")
    assert response.status_code == 403
    assert _body(sqlite_engine, world)[0] == 1 and _body(sqlite_engine, world, ios)[0] == 1
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(eval_recommendations.c.state)).scalar() == "waiting"


def test_a_model_change_needs_an_admin_and_the_screen_says_so(sqlite_engine, keyring, clock, world, users):
    experiment = _experiment(sqlite_engine, world, variable="model", variant=SONNET)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "model": SONNET})
    effort = _experiment(sqlite_engine, world, specialist="security")
    _recommendation(sqlite_engine, world, effort, {"specialist": "security", "effort": "high"})
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    shown = lead.client.get(f"/api/teams/{world.team}/insights/models", params={"days": "30"}).json()
    assert {r["change"].get("model", "effort"): r["can_approve"] for r in shown["recommendations"]} == {
        SONNET: False, "effort": True}
    refused = _post(lead, world, f"/recommendations/{rec_id}/approve")
    assert refused.status_code == 403 and refused.json()["error"] == "A model change needs an admin to approve."
    assert _body(sqlite_engine, world)[0] == 1
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    seen = admin.client.get(f"/api/teams/{world.team}/insights/models", params={"days": "30"}).json()
    assert all(r["can_approve"] for r in seen["recommendations"])


def test_an_admin_approving_a_model_change_adds_a_specialist_alias_on_the_review_provider(sqlite_engine, keyring,
                                                                                        clock, world, users):
    experiment = _experiment(sqlite_engine, world, variable="model", variant=SONNET)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "model": SONNET})
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    response = _post(admin, world, f"/recommendations/{rec_id}/approve")
    assert response.status_code == 200, response.text
    assert response.json()["applied"][0]["after"] == {"model": "review-concurrency", "effort": None}
    _, body = _body(sqlite_engine, world)
    assert experiments.agent_stage(body, "concurrency")["model"] == "review-concurrency"
    assert experiments.agent_stage(body, "security")["model"] == "review"
    with sqlite_engine.connect() as conn:
        aliases = conn.execute(select(providers.c.aliases)).scalar()
        assert conn.execute(select(audit).where(audit.c.action == "provider.update")).first() is not None
    assert aliases == {"review": OPUS, "fast": "claude-haiku-4-5", "review-concurrency": SONNET}


def test_a_model_an_alias_already_names_reuses_that_alias(sqlite_engine, keyring, clock, world, users):
    with sqlite_engine.begin() as conn:
        conn.execute(update(providers).values(aliases={"review": OPUS, "sonnet": SONNET}))
    experiment = _experiment(sqlite_engine, world, variable="model", variant=SONNET)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "model": SONNET})
    response = _post(_client(sqlite_engine, keyring, clock, users["admin"]), world,
                     f"/recommendations/{rec_id}/approve")
    assert response.json()["applied"][0]["after"]["model"] == "sonnet"
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(providers.c.aliases)).scalar() == {"review": OPUS, "sonnet": SONNET}


def test_dismissing_keeps_the_workflow_and_records_the_reason(sqlite_engine, keyring, clock, world, users):
    experiment = _experiment(sqlite_engine, world)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "effort": "high"})
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    response = _post(lead, world, f"/recommendations/{rec_id}/dismiss", {"reason": "  Not this quarter.  "})
    assert response.status_code == 200 and response.json()["recommendation"]["state"] == "dismissed"
    assert _body(sqlite_engine, world)[0] == 1
    with sqlite_engine.connect() as conn:
        logged = conn.execute(select(audit).where(audit.c.action == "insights.recommendation.dismiss")).one()
    assert logged.detail["reason"] == "Not this quarter."
    assert _post(lead, world, f"/recommendations/{rec_id}/approve").status_code == 409
    outsider = _client(sqlite_engine, keyring, clock, users["outsider"])
    assert _post(outsider, world, f"/recommendations/{rec_id}/dismiss").status_code in (403, 404)


def test_experiments_are_created_listed_and_moved_by_team_leads_only(sqlite_engine, keyring, clock, world, users):
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    member = _client(sqlite_engine, keyring, clock, users["member"])
    body = {"specialist": "concurrency", "variable": "effort", "variant": "high"}
    assert _post(member, world, "/experiments", body).status_code == 403
    assert member.client.get(f"/api/teams/{world.team}/insights/models/experiments").status_code == 403
    created = _post(lead, world, "/experiments", body)
    assert created.status_code == 200, created.text
    shown = created.json()["experiment"]
    assert (shown["status"], shown["sample_rate"], shown["daily_budget_usd"], shown["min_prs"]) == (
        "running", 0.2, 25.0, 30)
    assert shown["question"] == "Concurrency at high effort instead of the default effort"
    assert _post(lead, world, "/experiments", body).status_code == 409
    for bad in ({**body, "specialist": "team_lead"}, {**body, "variant": "huge"}, {**body, "variable": "tier"},
                {"specialist": "security", "variable": "model", "variant": OPUS}, {**body, "sample_rate": 2},
                {**body, "specialist": "security", "max_concurrent_arms": 1}):
        assert _post(lead, world, "/experiments", bad).status_code == 400, bad
    listed = lead.client.get(f"/api/teams/{world.team}/insights/models/experiments").json()
    assert [e["id"] for e in listed["experiments"]] == [shown["id"]]
    assert "concurrency" in listed["specialists"] and "xhigh" in listed["efforts"]
    assert listed["current"]["concurrency"] == {"alias": "review", "model": OPUS, "effort": None,
                                                "models": sorted(["claude-haiku-4-5", OPUS])}
    assert listed["defaults"]["daily_budget_usd"] == 25 and listed["lead_limits"] == experiments.LEAD_LIMITS
    for action, status in (("pause", "paused"), ("resume", "running"), ("finish", "finished")):
        moved = _post(lead, world, f"/experiments/{shown['id']}/{action}")
        assert moved.status_code == 200 and moved.json()["experiment"]["status"] == status
    assert _post(lead, world, f"/experiments/{shown['id']}/resume").status_code == 409
    assert _post(lead, world, f"/experiments/{shown['id']}/explode").status_code == 404
    assert _post(member, world, f"/experiments/{shown['id']}/pause").status_code == 403
    with sqlite_engine.connect() as conn:
        actions = [r.action for r in conn.execute(select(audit.c.action).where(
            audit.c.action.like("insights.experiment.%")).order_by(audit.c.at))]
    assert actions == ["insights.experiment.create", "insights.experiment.pause", "insights.experiment.resume",
                       "insights.experiment.finish"]


def test_a_team_lead_compares_only_models_the_provider_names_and_within_lead_limits(sqlite_engine, keyring, clock,
                                                                                    world, users):
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    unnamed = {"specialist": "security", "variable": "model", "variant": SONNET}
    refused = _post(lead, world, "/experiments", unnamed)
    assert refused.status_code == 403 and "aliases already names" in refused.json()["error"]
    named = {"specialist": "simplicity", "variable": "model", "variant": "claude-haiku-4-5"}
    assert _post(lead, world, "/experiments", named).status_code == 200
    for over in ({"sample_rate": 0.6}, {"max_concurrent_arms": 6}):
        body = {"specialist": "correctness", "variable": "effort", "variant": "high", **over}
        assert _post(lead, world, "/experiments", body).status_code == 403, over
    assert _post(admin, world, "/experiments", unnamed).status_code == 200
    assert _post(admin, world, "/experiments", {"specialist": "correctness", "variable": "effort", "variant": "high",
                                                "sample_rate": 0.9, "max_concurrent_arms": 8}).status_code == 200


def _gateway_provider(engine, world):
    """Another provider of the team, whose alias gw names a model the review's provider doesn't serve."""
    gateway = new_id()
    with engine.begin() as conn:
        conn.execute(insert(providers).values(id=gateway, team_id=world.team, kind="bedrock",
                                              config={"region": "us-east-1"}, aliases={"gw": "gateway-model"},
                                              created_at=NOW, updated_at=NOW))
    return gateway


def test_a_variant_alias_on_another_provider_needs_an_admin(sqlite_engine, keyring, clock, world, users):
    _gateway_provider(sqlite_engine, world)
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    body = {"specialist": "concurrency", "variable": "model", "alias": "gw"}
    refused = _post(lead, world, "/experiments", body)
    assert refused.status_code == 403 and "another provider" in refused.json()["error"]
    for bad in ({**body, "alias": "nope"}, {**body, "alias": "review"}, {**body, "alias": "Not An Alias"}):
        assert _post(admin, world, "/experiments", bad).status_code == 400, bad
    created = _post(admin, world, "/experiments", body)
    assert created.status_code == 200, created.text
    assert created.json()["experiment"]["variant"] == {"model": "gateway-model", "alias": "gw"}
    same = _post(lead, world, "/experiments", {"specialist": "security", "variable": "model", "alias": "fast"})
    assert same.status_code == 200, same.text
    assert same.json()["experiment"]["variant"] == {"model": "claude-haiku-4-5", "alias": "fast"}
    shown = admin.client.get(f"/api/teams/{world.team}/insights/models/experiments").json()["current"]
    assert shown["concurrency"]["aliases"] == [{"alias": "gw", "model": "gateway-model", "provider": "bedrock"}]
    assert "aliases" not in lead.client.get(f"/api/teams/{world.team}/insights/models/experiments").json()[
        "current"]["concurrency"]


def _project_provider(engine, team, project, aliases, **config):
    with engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=team, project_id=project, kind="bedrock",
                                              config={"region": "us-east-1", **config}, aliases=aliases,
                                              created_at=NOW, updated_at=NOW))


def test_a_variant_alias_resolves_in_each_review_pass_project(sqlite_engine, keyring, clock, world, users):
    _project_provider(sqlite_engine, world.team, world.project, {"review": OPUS, "quick": SONNET})
    lead = _client(sqlite_engine, keyring, clock, users["lead"])
    same = _post(lead, world, "/experiments", {"specialist": "concurrency", "variable": "model", "alias": "quick"})
    assert same.status_code == 200, same.text
    assert same.json()["experiment"]["variant"] == {"model": SONNET, "alias": "quick"}
    _gateway_provider(sqlite_engine, world)
    other, _ = _second_project(sqlite_engine, world)
    _project_provider(sqlite_engine, world.team, other, {"gw": "other-model"})
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    split = _post(admin, world, "/experiments", {"specialist": "security", "variable": "model", "alias": "gw"})
    assert split.status_code == 400 and "another project" in split.json()["error"]


def test_a_gateway_alias_needs_a_price_before_it_is_compared(sqlite_engine, keyring, clock, world, users):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=world.team, kind="openai-compatible",
                                              config={"api_base": "https://gw.example.com"},
                                              aliases={"gw": "gateway-model", "priced": "priced-model"},
                                              created_at=NOW, updated_at=NOW))
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    unpriced = _post(admin, world, "/experiments", {"specialist": "concurrency", "variable": "model", "alias": "gw"})
    assert unpriced.status_code == 400
    assert unpriced.json()["error"] == "Set a price for gw on its provider first, so its cost can be compared."
    with sqlite_engine.begin() as conn:
        conn.execute(update(providers).where(providers.c.kind == "openai-compatible").values(
            config={"api_base": "https://gw.example.com", "prices": {"gw": {"input": 1, "output": 4}}}))
    priced = _post(admin, world, "/experiments", {"specialist": "concurrency", "variable": "model", "alias": "gw"})
    assert priced.status_code == 200, priced.text


def test_the_same_model_on_another_provider_is_named_by_alias(sqlite_engine, world):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(providers).values(id=new_id(), team_id=world.team, kind="bedrock",
                                              config={"region": "us-east-1"}, aliases={"mirror": OPUS},
                                              created_at=NOW, updated_at=NOW))
    experiment = _experiment(sqlite_engine, world, variable="model", alias="mirror")
    assert scoring.question(experiment) == "Concurrency on Claude Opus 4.6 (mirror) instead of Claude Opus 4.6 (review)"
    assert scoring.change(experiment) == {"specialist": "concurrency", "model": OPUS, "alias": "mirror"}


def test_a_variant_alias_on_another_provider_runs_its_arm_on_that_alias(sqlite_engine, world):
    _gateway_provider(sqlite_engine, world)
    experiment = _experiment(sqlite_engine, world, variable="model", alias="gw")
    risk = {"level": "medium", "head": HEAD}
    assert experiments.overrides(experiment, "variant", OPUS, risk)["concurrency"] == {
        "model": "gateway-model", "alias": "gw"}
    assert experiments.overrides(experiment, "control", OPUS, risk)["concurrency"] == {"model": OPUS}


def test_approving_a_variant_on_another_provider_switches_to_its_alias(sqlite_engine, keyring, clock, world, users):
    _gateway_provider(sqlite_engine, world)
    experiment = _experiment(sqlite_engine, world, variable="model", alias="gw")
    rec_id = _recommendation(sqlite_engine, world, experiment, scoring.change(experiment))
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    response = _post(admin, world, f"/recommendations/{rec_id}/approve")
    assert response.status_code == 200, response.text
    assert response.json()["applied"][0]["after"] == {"model": "gw", "effort": None}
    _, body = _body(sqlite_engine, world)
    assert experiments.agent_stage(body, "concurrency")["model"] == "gw"
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(providers.c.aliases).where(providers.c.kind == "anthropic")).scalar() == {
            "review": OPUS, "fast": "claude-haiku-4-5"}
        assert conn.execute(select(audit).where(audit.c.action == "provider.update")).first() is None


def test_a_variant_alias_that_names_another_model_since_is_not_applied(sqlite_engine, keyring, clock, world, users):
    gateway = _gateway_provider(sqlite_engine, world)
    experiment = _experiment(sqlite_engine, world, variable="model", alias="gw")
    rec_id = _recommendation(sqlite_engine, world, experiment, scoring.change(experiment))
    with sqlite_engine.begin() as conn:
        conn.execute(update(providers).where(providers.c.id == gateway).values(aliases={"gw": "newer-model"}))
    response = _post(_client(sqlite_engine, keyring, clock, users["admin"]), world,
                     f"/recommendations/{rec_id}/approve")
    assert response.status_code == 409 and _body(sqlite_engine, world)[0] == 1


def test_the_models_screen_and_a_comparisons_detail(sqlite_engine, keyring, clock, world, users):
    experiment = _experiment(sqlite_engine, world, min_prs=5)
    _scored(sqlite_engine, world, experiment, range(1, 6))
    with sqlite_engine.begin() as conn:
        scoring.store(conn, experiment, scoring.compute(conn, experiment), NOW)
    scoring.recommend(sqlite_engine, world.team, NOW)
    member = _client(sqlite_engine, keyring, clock, users["member"])
    models = member.client.get(f"/api/teams/{world.team}/insights/models", params={"days": "30"}).json()
    [row] = models["comparisons"]
    assert row["question"] == "Concurrency at high effort instead of the default effort"
    assert (row["prs"], row["min_prs"], row["result"], row["cost_share"]) == (
        5, 5, {"kind": "cheaper", "words": "Found as many real issues, within normal variation, at 60% of the cost"},
        0.6)
    [waiting] = models["recommendations"]
    assert waiting["sentence"].startswith("Over 5 pull requests") and waiting["experiment_id"] == experiment.id
    assert models["comparison_spend_usd"]["value"] == pytest.approx(8.0)
    assert models["spend_usd"]["value"] == pytest.approx(5.0)
    detail = member.client.get(f"/api/teams/{world.team}/insights/models/comparisons/{experiment.id}").json()
    # The drawer compares the variant with the control, which ran the current setting alongside it.
    assert detail["prs"] == 5 and detail["found"] == {"both": 5, "only_current": 0, "only_variant": 0}
    assert detail["valid"] == {"both": 5, "only_current": 0, "only_variant": 0}
    assert detail["sides"]["current"]["name"] == "the default effort" and detail["sides"]["variant"]["name"] == (
        "high effort")
    assert (detail["sides"]["variant"]["cost_usd"], detail["sides"]["current"]["raised"]) == (0.6, 5)
    assert detail["sides"]["review"]["raised"] == 10
    assert detail["effect"]["cost_usd"]["mean"] == pytest.approx(-0.4) and detail["effect"]["cost_usd"]["significant"]
    assert detail["environment_shift"]["recall"]["mean"] == pytest.approx(-0.5)
    assert detail["result"]["words"] == row["result"]["words"]
    assert len(detail["points"]) == 5 and detail["points"][0]["pr"].startswith("acme/app#")
    assert detail["arms"]["done"] == 10
    outsider = _client(sqlite_engine, keyring, clock, users["outsider"])
    assert outsider.client.get(
        f"/api/teams/{world.other}/insights/models/comparisons/{experiment.id}").status_code == 404


# Review round 1.

def test_the_control_pins_the_model_the_review_actually_ran(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world, model=None)
    with sqlite_engine.begin() as conn:
        _spend(conn, world.team, run_id, 0.9, model="us.anthropic.claude-opus-4-6-v1")
        _spend(conn, world.team, run_id, 0.0, model="claude-cache-only", cached=True)
    experiments.queue_arms(sqlite_engine, experiment, NOW)
    found = _arms(sqlite_engine, experiment.id, run_id)
    assert found["control"].overrides["concurrency"] == {"model": "us.anthropic.claude-opus-4-6-v1"}
    assert found["variant"].overrides["concurrency"]["model"] == "us.anthropic.claude-opus-4-6-v1"


def test_only_reviews_that_ran_the_baseline_setting_are_pooled(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    other = primary(sqlite_engine, world, model=None)
    with sqlite_engine.begin() as conn:
        _spend(conn, world.team, other, 1.0, effort="xhigh")
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 0, "skipped": 2}
    assert {a.reason for a in _arms(sqlite_engine, experiment.id, other).values()} == {
        "This review ran concurrency with another setting than the comparison's baseline."}


def test_a_head_reviewed_twice_is_compared_once(sqlite_engine, world):
    _runner(sqlite_engine)
    experiment = _experiment(sqlite_engine, world)
    first = primary(sqlite_engine, world, queued=NOW - timedelta(minutes=50))
    again = primary(sqlite_engine, world, queued=NOW - timedelta(minutes=40))
    assert experiments.queue_arms(sqlite_engine, experiment, NOW)["queued"] == 2
    assert set(_arms(sqlite_engine, experiment.id, first)) == {"variant", "control"}
    assert _arms(sqlite_engine, experiment.id, again) == {}
    assert experiments.queue_arms(sqlite_engine, experiment, NOW) == {"queued": 0, "skipped": 0}


def test_the_matcher_window_ends_fifteen_lines_away(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    variant = _done_arm(sqlite_engine, world, experiment, run_id, "variant")
    with sqlite_engine.begin() as conn:
        anchor = _candidate(conn, world, run_id, 0, "A.kt", 10, LOCK)
        near = _candidate(conn, world, variant, 0, "A.kt", 25, "the lock is never released when the parse throws")
        far = _candidate(conn, world, variant, 1, "A.kt", 26, "the lock is never released when the parse throws")
        arm_matching.match(conn, experiment, run_id)
        rows = {r.left_candidate_id: r.right_candidate_id for r in conn.execute(select(eval_matches))}
    assert rows == {near: anchor, far: None}


def test_the_better_of_two_near_findings_wins_the_pair(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world)
    variant = _done_arm(sqlite_engine, world, experiment, run_id, "variant")
    with sqlite_engine.begin() as conn:
        anchor = _candidate(conn, world, run_id, 0, "A.kt", 10, "lock never released on the error path")
        weaker = _candidate(conn, world, variant, 0, "A.kt", 11, "lock never released when the error happens later")
        stronger = _candidate(conn, world, variant, 1, "A.kt", 12, "the lock is never released on the error path")
        arm_matching.match(conn, experiment, run_id)
        rows = {r.left_candidate_id: r.right_candidate_id for r in conn.execute(select(eval_matches))}
    assert rows == {stronger: anchor, weaker: None}


def test_the_budget_day_starts_at_utc_midnight(sqlite_engine, world):
    experiment = _experiment(sqlite_engine, world)
    run_id = primary(sqlite_engine, world, number=5)
    arm_run = _done_arm(sqlite_engine, world, experiment, run_id, "variant")
    midnight = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    with sqlite_engine.begin() as conn:
        _spend(conn, world.team, arm_run, 2.0, at=midnight - timedelta(minutes=1))
        _spend(conn, world.team, arm_run, 0.5, at=midnight + timedelta(minutes=1))
        assert arms.spent_today(conn, experiment.id, midnight + timedelta(minutes=30)) == Decimal("0.5")
        assert arms.spent_today(conn, experiment.id, midnight - timedelta(minutes=1)) >= Decimal("2.0")


def test_approve_refuses_a_step_changed_since_the_baseline_and_applies_nothing(sqlite_engine, keyring, clock, world,
                                                                              users):
    experiment = _experiment(sqlite_engine, world)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "effort": "high"})
    _, ios = _second_project(sqlite_engine, world)
    with sqlite_engine.begin() as conn:
        row = conn.execute(select(workflows).where(workflows.c.id == ios)).one()
        body = defs.set_agent_field(starters.body("review-pass"), "concurrency", "effort", "low")
        defs._add_version(conn, defs.WORKFLOW, ios, 2, body, "hand edit", None, NOW)
        conn.execute(update(workflows).where(workflows.c.id == row.id).values(current_version=2))
    response = _post(_client(sqlite_engine, keyring, clock, users["lead"]), world, f"/recommendations/{rec_id}/approve")
    assert response.status_code == 409 and "changed since the comparison began" in response.json()["error"]
    assert _body(sqlite_engine, world)[0] == 1 and _body(sqlite_engine, world, ios)[0] == 2


def test_a_failure_saving_the_second_row_rolls_the_first_back(sqlite_engine, keyring, clock, world, users,
                                                              monkeypatch):
    experiment = _experiment(sqlite_engine, world)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "effort": "high"})
    _, ios = _second_project(sqlite_engine, world)
    real, saves = defs.save, []

    def failing(*args, **kwargs):
        saves.append(args[2].id)
        if len(saves) == 2:
            raise defs.DefError(409, "conflict")
        return real(*args, **kwargs)
    monkeypatch.setattr(defs, "save", failing)
    response = _post(_client(sqlite_engine, keyring, clock, users["lead"]), world, f"/recommendations/{rec_id}/approve")
    assert response.status_code == 409 and len(saves) == 2
    assert _body(sqlite_engine, world)[0] == 1 and _body(sqlite_engine, world, ios)[0] == 1
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(eval_recommendations.c.state)).scalar() == "waiting"


def test_a_recommendation_someone_else_decided_first_is_refused(sqlite_engine, world):
    from armature.server.insights import recommendations
    experiment = _experiment(sqlite_engine, world)
    rec_id = _recommendation(sqlite_engine, world, experiment, {"specialist": "concurrency", "effort": "high"})
    with sqlite_engine.begin() as conn:
        conn.execute(update(eval_recommendations).values(state="dismissed"))
        with pytest.raises(recommendations.RecommendationError) as refused:
            recommendations._decide(conn, rec_id, state="applied")
    assert refused.value.status == 409


def test_one_server_works_an_experiment_each_tick(sqlite_engine, keyring, world, monkeypatch):
    experiment = _experiment(sqlite_engine, world)
    worked = []
    monkeypatch.setattr(experiments, "queue_arms", lambda engine, row, now: worked.append(row.id))
    monkeypatch.setattr(experiments, "evaluate", lambda *args, **kwargs: None)
    first = experiments.Arms(sqlite_engine, keyring, clock=lambda: NOW)
    second = experiments.Arms(sqlite_engine, keyring, clock=lambda: NOW)
    first.tick()
    second.tick()
    assert worked == [experiment.id]
    later = experiments.Arms(sqlite_engine, keyring, clock=lambda: NOW + timedelta(seconds=experiments.TICK_S))
    later.tick()
    assert worked == [experiment.id, experiment.id]


def test_a_tick_slot_never_shows_in_the_settings_list(sqlite_engine, keyring, clock, world, users, monkeypatch):
    _experiment(sqlite_engine, world)
    monkeypatch.setattr(experiments, "queue_arms", lambda *args: None)
    monkeypatch.setattr(experiments, "evaluate", lambda *args, **kwargs: None)
    experiments.Arms(sqlite_engine, keyring, clock=lambda: NOW).tick()
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(settings.c.key).where(settings.c.key.like("_insights.arms.%"))).first()
    admin = _client(sqlite_engine, keyring, clock, users["admin"])
    listed = admin.client.get("/api/settings", params={"scope": world.team}).json()["settings"]
    assert not [s for s in listed if s["key"].startswith("_insights.")]
