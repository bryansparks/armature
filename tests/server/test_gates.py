"""Gates: a run pauses at a human gate, a decision resumes the same run, and a gate nobody decides expires."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from armature.events.redact import REDACTED
from armature.events.register import registration_body
from armature.events.sign import sign_engine
from armature.nodes.gate import gate_digest
from armature.runner.client import Client, Runner
from armature.runner.process import armature_binary
from armature.server import defs, dispatch, gates, runners, secrets, worker
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    jobs,
    projects,
    run_events,
    run_tokens,
    runs,
    settings,
    teams,
)
from armature.server.db.schema import gates as gate_rows
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.live_support import LiveServer

REPO = Path(__file__).resolve().parents[2]

GATED = """name: ship
stages:
  - id: build
    adapter: build
    depends_on: []
  - id: approve
    gate: human
    present: "Ship **{{{{ build.stdout | trim }}}}**?"
    depends_on: [build]
  - id: ship
    adapter: ship
    condition: "{{{{ approve.approved }}}}"
    depends_on: [approve]
adapters:
  build:
    name: build
    type: script
    cmd: "echo built >> {marker} && echo v1"
  ship:
    name: ship
    type: script
    cmd: "echo shipped >> {marker}"
"""

REQUEST = {"step_id": "approve", "key": "approve", "present": "Ship v1?", "checkpoint": {"build": {"content": "v1"}}}


def _wait(check, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def world(sqlite_engine, keyring, tmp_path):
    clock = Clock()
    team, project, other_project, workflow = new_id(), new_id(), new_id(), new_id()
    marker = tmp_path / "marker.txt"
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", created_at=clock()))
        conn.execute(insert(projects).values([
            {"id": project, "team_id": team, "name": "app", "created_at": clock()},
            {"id": other_project, "team_id": team, "name": "web", "created_at": clock()}]))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="ship", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, GATED.format(marker=marker), "seed", None, clock())
        conn.execute(insert(settings).values([{"team_id": None, "key": "worker.enabled", "value": False},
                                              {"team_id": team, "key": "env.PYTHONPATH", "value": str(REPO)}]))
    users = {}
    for name, kind, scope, role in (("lead", "project", project, "project_lead"),
                                    ("viewer", "project", project, "viewer"),
                                    ("elsewhere", "project", other_project, "project_lead"),
                                    ("teamlead", "team", team, "team_lead")):
        users[name] = add_user(sqlite_engine, keyring, clock, name)[0]
        add_membership(sqlite_engine, clock, users[name], kind, scope, role)
    return SimpleNamespace(engine=sqlite_engine, keyring=keyring, clock=clock, team=team, project=project,
                           workflow=workflow, marker=marker, users=users)


def _workflow(world, name, body):
    workflow = new_id()
    with world.engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name=name, current_version=1,
                                                        created_at=world.clock(), updated_at=world.clock(),
                                                        **defs.DefScope("team", world.team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, body, "seed", None, world.clock())
    return workflow


def _queue(world, thread=None, workflow=None):
    with world.engine.begin() as conn:
        job_id = worker.queue_job(conn, team_id=world.team, project_id=world.project,
                                  workflow_id=workflow or world.workflow, inputs={}, now=world.clock(), thread=thread)
    world.clock.advance(seconds=1)
    return job_id


def _claim(world, job_id):
    with world.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(status="claimed"))
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).one()
        return worker.build_bundle(conn, world.keyring, job, now=world.clock(), lease=worker.LEASE,
                                   events="unix:/nowhere")


def _signed(world, run_id, request=REQUEST):
    """A run_suspend payload as the run's engine signs it for this gate request."""
    step_id, key, digest = request.get("step_id"), request.get("key"), gate_digest(request)
    ts = world.clock().isoformat()
    signed = {"kind": "run_suspend", "run_id": run_id, "step_id": step_id, "key": key, "digest": digest, "ts": ts}
    return {"step_id": step_id, "key": key, "digest": digest, "ts": ts,
            "sig": sign_engine(runners.engine_key(world.keyring, run_id), signed)}


def _suspend(world, run_id, request=REQUEST):
    _event(world, run_id, "run_suspend", **_signed(world, run_id, request))


def _open(world, job_id, request, *, sent=True):
    """Open a gate as a host would after the run sent a matching run_suspend and then exited."""
    if sent:
        _suspend(world, _row(world, jobs, job_id).run_id, request)
    with world.engine.begin() as conn:
        return gates.open_gate(conn, world.keyring, job_id=job_id, request=request, now=world.clock())


def _paused(world, thread=None, workflow=None, **request):
    job_id = _queue(world, thread, workflow)
    bundle = _claim(world, job_id)
    return job_id, bundle, _open(world, job_id, {**REQUEST, **request})


def _row(world, table, row_id):
    with world.engine.connect() as conn:
        return conn.execute(select(table).where(table.c.id == row_id)).one()


def _gate(world, gate_id):
    return _row(world, gate_rows, gate_id)


def _event(world, run_id, kind, **payload):
    with world.engine.begin() as conn:
        seq = conn.execute(select(func.coalesce(func.max(run_events.c.seq), 0))
                           .where(run_events.c.run_id == run_id)).scalar_one() + 1
        conn.execute(insert(run_events).values(run_id=run_id, seq=seq, ts=world.clock(), kind=kind, payload=payload))


def _decide(world, gate_id, approved=True, nonce=None, user="lead"):
    with world.engine.begin() as conn:
        return gates.decide(conn, gate_id=gate_id, approved=approved, nonce=nonce or _gate(world, gate_id).nonce,
                            actor_id=world.users[user], via="web", note="looks fine", now=world.clock())


def test_a_paused_run_waits_with_its_job_suspended_and_its_token_gone(world):
    job_id, bundle, gate_id = _paused(world, approval=None)
    gate, job, run = _gate(world, gate_id), _row(world, jobs, job_id), _row(world, runs, bundle.run_id)
    assert (gate.state, gate.step_id, gate.min_role, gate.present) == ("open", "approve", "project_lead", "Ship v1?")
    assert gate.expires_at == world.clock() + timedelta(hours=72)
    assert json.loads(gate.checkpoint) == {"build": {"content": "v1"}} and gate.workflow_version == 1
    assert job.status == "suspended" and run.status == "waiting" and run.finished_at is None
    with world.engine.connect() as conn:
        assert conn.execute(select(run_tokens).where(run_tokens.c.run_id == bundle.run_id)).first() is None
        assert conn.execute(select(audit.c.action).where(audit.c.target_id == gate_id)).scalars().all() \
            == ["gate.open"]
    assert bundle.checkpoint == {}


def test_the_checkpoint_and_question_are_redacted(world):
    with world.engine.begin() as conn:
        secrets.set_secret(conn, world.keyring, team_id=world.team, name="TOKEN", value="hunter2-value",
                           actor_id=None)
    _, _, gate_id = _paused(world, present="token hunter2-value", checkpoint={"build": {"content": "hunter2-value"}})
    gate = _gate(world, gate_id)
    assert "hunter2" not in gate.checkpoint and "hunter2" not in gate.present and REDACTED in gate.present


def _approval(world, approval):
    """The ship workflow with an ``approval:`` block on its gate."""
    gate = "    gate: human\n"
    body = GATED.format(marker=world.marker).replace(gate, f"{gate}    approval: {approval}\n")
    return _workflow(world, f"ship-{new_id()[:8]}", body)


def test_role_and_expiry_come_from_the_pinned_workflow_never_the_request(world):
    claimed = {"min_role": "admin", "expires_hours": 700}
    _, _, gate_id = _paused(world, workflow=_approval(world, "{min_role: team_lead, expires_hours: 2}"), **claimed)
    gate = _gate(world, gate_id)
    assert gate.min_role == "team_lead" and gate.expires_at == world.clock() + timedelta(hours=2)
    _, _, plain = _paused(world, **claimed)
    gate = _gate(world, plain)
    assert gate.min_role == "project_lead" and gate.expires_at == world.clock() + timedelta(hours=72)


def test_approval_resumes_the_same_run_from_the_checkpoint(world):
    job_id, bundle, gate_id = _paused(world, thread="slack:C1:1700.1")
    decided = _decide(world, gate_id)
    gate = _gate(world, gate_id)
    resume = _row(world, jobs, gate.resume_job_id)
    assert decided["state"] == "approved" and gate.decided_by == world.users["lead"] and gate.decided_via == "web"
    assert (resume.status, resume.run_id, resume.resume_of_gate) == ("queued", bundle.run_id, gate_id)
    assert (resume.thread, resume.workflow_version) == ("slack:C1:1700.1", 1)
    resumed = _claim(world, resume.id)
    assert resumed.run_id == bundle.run_id and resumed.thread == "slack:C1:1700.1"
    assert resumed.env()[worker.THREAD_ENV] == "slack:C1:1700.1"
    assert resumed.checkpoint["build"] == {"content": "v1"}
    assert resumed.checkpoint["approve"]["approved"] is True
    assert resumed.checkpoint["approve"]["feedback"] == "looks fine"
    run = _row(world, runs, bundle.run_id)
    assert run.status == "running" and run.thread == "slack:C1:1700.1"
    with world.engine.connect() as conn:
        assert conn.execute(select(run_tokens).where(run_tokens.c.run_id == bundle.run_id)).first() is not None
        assert "gate.approve" in conn.execute(select(audit.c.action).where(audit.c.target_id == gate_id)).scalars()
    assert _row(world, jobs, job_id).status == "suspended"


def test_a_rejection_resumes_with_approved_false(world):
    _, _, gate_id = _paused(world)
    assert _decide(world, gate_id, approved=False)["state"] == "rejected"
    resume = _row(world, jobs, _gate(world, gate_id).resume_job_id)
    assert _claim(world, resume.id).checkpoint["approve"]["approved"] is False
    run = _row(world, runs, _gate(world, gate_id).run_id)
    assert (run.outcome, run.outcome_source) == ("blocked", "server")


def test_of_two_racing_approvals_one_wins(world):
    _, _, gate_id = _paused(world)
    nonce = _gate(world, gate_id).nonce
    start = threading.Barrier(2)
    outcomes = []

    def approve(user):
        start.wait()
        try:
            _decide(world, gate_id, nonce=nonce, user=user)
            outcomes.append("won")
        except gates.GateError as exc:
            outcomes.append(exc.status)

    racers = [threading.Thread(target=approve, args=(u,)) for u in ("lead", "teamlead")]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(30)
    assert sorted(map(str, outcomes)) == ["409", "won"]
    with world.engine.connect() as conn:
        assert len(conn.execute(select(jobs).where(jobs.c.resume_of_gate == gate_id)).all()) == 1


def test_a_stale_nonce_is_refused(world):
    _, _, gate_id = _paused(world)
    with pytest.raises(gates.GateError) as refused:
        _decide(world, gate_id, nonce="0" * 64)
    assert refused.value.status == 409 and _gate(world, gate_id).state == "open"


def test_an_expired_gate_is_rejected_and_resumes(world, tmp_path):
    _, _, gate_id = _paused(world, workflow=_approval(world, "{expires_hours: 1}"))
    nonce = _gate(world, gate_id).nonce
    world.clock.advance(hours=1, seconds=1)
    with pytest.raises(gates.GateError):
        _decide(world, gate_id, nonce=nonce)
    cfg = SimpleNamespace(public_origin="https://arm.test", base_path="")
    cfg_worker = worker.Worker(world.engine, world.keyring, cfg, SimpleNamespace(), clock=world.clock,
                               work_dir=tmp_path / "work")
    cfg_worker.tick()
    gate = _gate(world, gate_id)
    assert (gate.state, gate.decision_note) == ("expired", "expired")
    answer = _claim(world, gate.resume_job_id).checkpoint["approve"]
    assert answer["approved"] is False and answer["feedback"] == "expired"
    assert _row(world, runs, gate.run_id).outcome == "blocked"
    with world.engine.begin() as conn:
        assert gates.expire(conn, world.clock()) == []


def test_canceling_a_suspended_job_cancels_its_run_and_gate(world):
    job_id, bundle, gate_id = _paused(world)
    with world.engine.begin() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == job_id)).one()
        dispatch.cancel(conn, job=job, actor_id=world.users["lead"], now=world.clock(), ip=None)
    assert _row(world, jobs, job_id).status == "canceled"
    assert _row(world, runs, bundle.run_id).status == "canceled" and _gate(world, gate_id).state == "canceled"


def test_a_gate_request_must_name_a_gate_stage_and_its_key(world):
    job_id = _queue(world)
    bundle = _claim(world, job_id)
    for bad in ({"step_id": "build", "key": "build"}, {"step_id": "nope", "key": "nope"},
                {"key": "build"}, {"key": "approve__iter_x"}, {"key": "approve__iter_0"}, {"key": "other"}):
        with pytest.raises(gates.GateError) as refused:
            _open(world, job_id, {**REQUEST, **bad}, sent=False)
        assert refused.value.status == 400, bad
    gate_id = _open(world, job_id, {**REQUEST, "key": "approve__iter_3"})
    assert _gate(world, gate_id).step_key == "approve__iter_3"
    assert _row(world, runs, bundle.run_id).status == "waiting"


def test_an_uploaded_gate_answer_is_dropped_and_only_decided_rows_answer(world):
    forged = {"build": {"stdout": "v1"}, "approve": {"approved": True}, "approve__iter_2": {"approved": True}}
    _, _, gate_id = _paused(world, checkpoint=forged)
    assert json.loads(_gate(world, gate_id).checkpoint) == {"build": {"stdout": "v1"}}
    _decide(world, gate_id, approved=False)
    resumed = _claim(world, _gate(world, gate_id).resume_job_id).checkpoint
    assert resumed["approve"]["approved"] is False and "approve__iter_2" not in resumed


TWO_GATES = """name: two
stages:
  - id: approve
    gate: human
  - id: release
    gate: human
    depends_on: [approve]
"""


def test_a_gate_request_must_match_the_pause_the_run_sent(world):
    job_id = _queue(world, workflow=_workflow(world, "two", TWO_GATES))
    bundle = _claim(world, job_id)
    _suspend(world, bundle.run_id)
    for forged in ({"key": "approve__iter_9"}, {"step_id": "release", "key": "release"}):
        with pytest.raises(gates.GateError) as refused:
            _open(world, job_id, {**REQUEST, **forged}, sent=False)
        assert refused.value.status == 409 and "doesn't match" in refused.value.message, forged
    for swapped in ({"checkpoint": {"build": {"content": "v1"}, "ship": {"stdout": "planted"}}},
                    {"checkpoint": {"build": {"content": "v2"}}}, {"present": "Ship it, all clear?"}):
        with pytest.raises(gates.GateError) as refused:
            _open(world, job_id, {**REQUEST, **swapped}, sent=False)
        assert refused.value.status == 409 and "isn't the one" in refused.value.message, swapped
    gate_id = _open(world, job_id, REQUEST, sent=False)
    assert _gate(world, gate_id).step_key == "approve"
    with world.engine.connect() as conn:
        assert not gates.suspend_reported(conn, world.keyring, bundle.run_id)


def test_only_a_run_suspend_the_engine_signed_opens_a_gate(world):
    job_id = _queue(world)
    bundle = _claim(world, job_id)
    assert bundle.engine_key == f"{bundle.run_id}:{runners.engine_key(world.keyring, bundle.run_id)}"
    assert bundle.engine_key.partition(":")[2] not in "".join(bundle.env().values())
    good = _signed(world, bundle.run_id)
    other_run = _signed(world, new_id())
    for forged in ({k: v for k, v in good.items() if k != "sig"}, {**good, "sig": "0" * 64},
                   {**good, "key": "approve__iter_2"}, {**good, "digest": "0" * 64}, {**other_run}):
        _event(world, bundle.run_id, "run_suspend", **forged)
        with pytest.raises(gates.GateError) as refused:
            _open(world, job_id, REQUEST, sent=False)
        assert refused.value.status == 409, forged
    with world.engine.connect() as conn:
        assert not gates.suspend_reported(conn, world.keyring, bundle.run_id)
    _event(world, bundle.run_id, "run_suspend", **good)
    assert _open(world, job_id, REQUEST, sent=False)


def test_outputs_after_the_gate_are_dropped_from_the_checkpoint(world):
    planted = {"build": {"stdout": "v1"}, "ship": {"stdout": "planted"}, "ship__iter_2": {}}
    _, _, gate_id = _paused(world, checkpoint=planted)
    assert json.loads(_gate(world, gate_id).checkpoint) == {"build": {"stdout": "v1"}}


def test_a_gate_for_a_run_that_already_ended_is_refused(world):
    job_id = _queue(world)
    bundle = _claim(world, job_id)
    with world.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == bundle.run_id).values(status="succeeded"))
    with pytest.raises(gates.GateError) as refused:
        _open(world, job_id, REQUEST)
    assert refused.value.status == 409
    with world.engine.connect() as conn:
        assert conn.execute(select(gate_rows)).first() is None


def _resume_job_left_waiting(world):
    _, bundle, gate_id = _paused(world)
    _decide(world, gate_id)
    return bundle.run_id, gate_id, _gate(world, gate_id).resume_job_id


def _assert_ended(world, run_id, gate_id):
    run = _row(world, runs, run_id)
    assert run.status == "failed" and run.finished_at is not None
    assert _gate(world, gate_id).checkpoint is None


def test_a_failed_resume_job_fails_its_waiting_run_in_the_worker(world, tmp_path):
    run_id, gate_id, resume = _resume_job_left_waiting(world)
    embedded = worker.Worker(world.engine, world.keyring, SimpleNamespace(), SimpleNamespace(), clock=world.clock,
                             work_dir=tmp_path / "work")
    assert embedded.try_claim(resume, world.clock())
    embedded._fail(resume, "The worker could not start this job.")
    _assert_ended(world, run_id, gate_id)


def test_a_failed_resume_job_fails_its_waiting_run_in_dispatch(world):
    run_id, gate_id, resume = _resume_job_left_waiting(world)
    with world.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == resume).values(status="claimed"))
    dispatch._fail(world.engine, resume, "The server could not prepare this job.", world.clock())
    _assert_ended(world, run_id, gate_id)


def test_a_reaped_job_fails_its_run_left_waiting_without_a_gate(world, tmp_path):
    job_id = _queue(world)
    bundle = _claim(world, job_id)
    with world.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == bundle.run_id).values(status="waiting"))
    world.clock.advance(minutes=10)
    embedded = worker.Worker(world.engine, world.keyring, SimpleNamespace(), SimpleNamespace(), clock=world.clock,
                             work_dir=tmp_path / "work")
    embedded.reap(world.clock())
    assert _row(world, jobs, job_id).status == "failed" and _row(world, runs, bundle.run_id).status == "failed"


def test_an_open_gate_keeps_its_run_waiting_when_another_job_fails(world, tmp_path):
    job_id, bundle, gate_id = _paused(world)
    with world.engine.begin() as conn:
        gates.fail_waiting(conn, job_id=job_id, error="x", now=world.clock())
    assert _row(world, runs, bundle.run_id).status == "waiting" and _gate(world, gate_id).checkpoint is not None


def test_a_pause_reported_after_a_cancel_request_cancels(world):
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                       now=world.clock(), ip=None)
        key_row = runners.authenticate(conn, world.keyring, secret, world.clock())
        runner = dispatch.hello(conn, key=key_row, body={"name": "r"}, now=world.clock())
    _queue(world)
    bundle = dispatch.claim(world.engine, world.keyring, key=key_row, runner_id=runner["runner_id"],
                            now=world.clock(), lease=worker.LEASE, events="x", bundle_version=3)
    with world.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == bundle.job_id).values(cancel_requested_at=world.clock()))
    status = dispatch.finish(world.engine, world.keyring, key=key_row, job_id=bundle.job_id, now=world.clock(),
                             body={"status": "suspended", "gate": REQUEST}, live_status="waiting")
    assert status == "canceled" and _row(world, runs, bundle.run_id).status == "canceled"
    with world.engine.connect() as conn:
        assert conn.execute(select(gate_rows)).first() is None


def test_a_pause_the_run_never_sent_fails(world):
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                       now=world.clock(), ip=None)
        key_row = runners.authenticate(conn, world.keyring, secret, world.clock())
        runner = dispatch.hello(conn, key=key_row, body={"name": "r"}, now=world.clock())
    _queue(world)
    claim = {"key": key_row, "runner_id": runner["runner_id"], "now": world.clock(), "lease": worker.LEASE,
             "events": "x", "bundle_version": 3}
    bundle = dispatch.claim(world.engine, world.keyring, **claim)
    status = dispatch.finish(world.engine, world.keyring, key=key_row, job_id=bundle.job_id, now=world.clock(),
                             body={"status": "suspended", "gate": REQUEST}, live_status="running")
    assert status == "failed" and _row(world, runs, bundle.run_id).status == "failed"
    _queue(world)
    second = dispatch.claim(world.engine, world.keyring, **claim)
    _suspend(world, second.run_id)
    status = dispatch.finish(world.engine, world.keyring, key=key_row, job_id=second.job_id, now=world.clock(),
                             body={"status": "suspended", "gate": REQUEST}, live_status=None)
    assert status == "suspended"


def test_a_checkpoint_is_pruned_once_its_run_ends(world):
    _, bundle, gate_id = _paused(world)
    _decide(world, gate_id)
    with world.engine.begin() as conn:
        assert gates.prune(conn) == 0
        conn.execute(update(runs).where(runs.c.id == bundle.run_id).values(status="succeeded"))
        assert gates.prune(conn) == 1
    assert _gate(world, gate_id).checkpoint is None


def test_a_resume_job_whose_run_was_canceled_fails(world):
    _, bundle, gate_id = _paused(world)
    _decide(world, gate_id)
    with world.engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == bundle.run_id).values(status="canceled"))
    with pytest.raises(worker.JobError, match="no longer waiting"):
        _claim(world, _gate(world, gate_id).resume_job_id)


def test_old_runners_never_claim_a_job_that_may_pause(world):
    _paused(world)
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                       now=world.clock(), ip=None)
        key_row = runners.authenticate(conn, world.keyring, secret, world.clock())
        runner = dispatch.hello(conn, key=key_row, body={"name": "old"}, now=world.clock())
    fresh = _queue(world)
    claim = {"key": key_row, "runner_id": runner["runner_id"], "now": world.clock(), "lease": worker.LEASE,
             "events": "x"}
    assert dispatch.claim(world.engine, world.keyring, bundle_version=2, **claim) is None
    assert dispatch.claim(world.engine, world.keyring, bundle_version=3, **claim).job_id == fresh


def test_a_runner_report_of_a_pause_opens_the_gate(world):
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                         now=world.clock(), ip=None)
        key_row = runners.authenticate(conn, world.keyring, secret, world.clock())
        runner = dispatch.hello(conn, key=key_row, body={"name": "r"}, now=world.clock())
    job_id = _queue(world)
    bundle = dispatch.claim(world.engine, world.keyring, key=key_row, runner_id=runner["runner_id"],
                            now=world.clock(), lease=worker.LEASE, events="x", bundle_version=3)
    assert bundle.job_id == job_id and bundle.checkpoint == {}
    _suspend(world, bundle.run_id)
    status = dispatch.finish(world.engine, world.keyring, key=key_row, job_id=job_id, now=world.clock(),
                             body={"status": "suspended", "gate": REQUEST}, live_status="waiting")
    assert status == "suspended" and _row(world, runs, bundle.run_id).status == "waiting"
    with world.engine.connect() as conn:
        assert conn.execute(select(gate_rows.c.state).where(gate_rows.c.run_id == bundle.run_id)).scalar() == "open"


def test_a_runner_reports_a_large_checkpoint_over_http(world):
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                       now=world.clock(), ip=None)
    _queue(world)
    auth = {"Authorization": f"Bearer {secret}"}
    with make_client(world.engine, world.keyring, world.clock) as app:
        runner_id = app.post("/api/runner/hello", json={"name": "r"}, headers=auth).json()["runner_id"]
        job = app.post("/api/runner/claim", json={"runner_id": runner_id, "bundle_version": 3}, headers=auth).json()
        gate = {**REQUEST, "checkpoint": {"build": {"stdout": "x" * 300_000}}}
        suspend = json.dumps({"v": 1, "kind": "run_suspend", "payload": _signed(world, job["job"]["run_id"], gate)})
        assert app.post("/api/events", content=suspend, headers={
            "Authorization": f"Bearer {job['job']['run_token']}"}).json()["accepted"] == 1
        answer = app.post(f"/api/runner/jobs/{job['job']['job_id']}/finish", headers=auth,
                          json={"status": "suspended", "gate": gate})
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"status": "suspended"} and job["job"]["checkpoint"] == {}


def test_a_pause_without_a_gate_request_fails_the_job_and_run(world):
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                         now=world.clock(), ip=None)
        key_row = runners.authenticate(conn, world.keyring, secret, world.clock())
        runner = dispatch.hello(conn, key=key_row, body={"name": "r"}, now=world.clock())
    _queue(world)
    bundle = dispatch.claim(world.engine, world.keyring, key=key_row, runner_id=runner["runner_id"],
                            now=world.clock(), lease=worker.LEASE, events="x", bundle_version=3)
    status = dispatch.finish(world.engine, world.keyring, key=key_row, job_id=bundle.job_id, now=world.clock(),
                             body={"status": "suspended", "gate": {"step_id": "approve"}}, live_status="waiting")
    assert status == "failed" and _row(world, runs, bundle.run_id).status == "failed"


def test_the_server_worker_opens_the_gate_its_run_left_and_resumes_from_the_checkpoint(world, tmp_path,
                                                                                        monkeypatch):
    from armature.nodes.gate import CHECKPOINT_FILE, GATE_DIR_ENV, write_gate
    from armature.server import dryrun
    launched = []

    def spawn(binary, launch, args, env, hub, **kwargs):
        launched.append({"launch": launch, "env": env, **kwargs})
        return SimpleNamespace(poll=lambda: None, pid=-1)
    monkeypatch.setattr(dryrun, "armature_binary", lambda: "/bin/armature")
    monkeypatch.setattr(dryrun, "spawn", spawn)
    cfg = SimpleNamespace(packs_dir="/srv/packs", public_origin="https://arm.test", base_path="", runner=SimpleNamespace(memory_mb=0))
    embedded = worker.Worker(world.engine, world.keyring, cfg, SimpleNamespace(unix_endpoint="/tmp/e.sock"),
                             clock=world.clock,
                             work_dir=tmp_path / "work")
    job_id = _queue(world, thread="web:1")
    assert embedded.try_claim(job_id, world.clock())
    embedded.launch(job_id)
    [first] = launched
    workdir = first["launch"].workdir
    assert first["env"][GATE_DIR_ENV] == str(workdir) and first["env"][worker.THREAD_ENV] == "web:1"
    secret = runners.engine_key(world.keyring, first["launch"].run_id)
    assert first["launch"].engine_key.endswith(secret) and secret not in "".join(first["env"].values())
    assert first["paused"]() is False
    write_gate(workdir, REQUEST)
    monkeypatch.setattr(dryrun, "END_WAIT_S", 0.3)
    assert first["paused"]() is False and _row(world, jobs, job_id).status == "running"
    _suspend(world, first["launch"].run_id)
    assert first["paused"]() is True
    gate = _row(world, gate_rows, select(gate_rows.c.id).where(gate_rows.c.run_id == first["launch"].run_id)
                .scalar_subquery())
    assert _row(world, jobs, job_id).status == "suspended" and gate.state == "open"
    _decide(world, gate.id)
    resume = _gate(world, gate.id).resume_job_id
    assert embedded.try_claim(resume, world.clock())
    embedded.launch(resume)
    second = launched[1]["launch"]
    assert second.run_id == first["launch"].run_id
    assert json.loads((second.workdir / CHECKPOINT_FILE).read_text())["approve"]["approved"] is True


# Routes and roles

@pytest.fixture
def app(world):
    return make_client(world.engine, world.keyring, world.clock)


def _as(app, world, user):
    return mutation(sign_in_as(app, world.engine, world.keyring, world.clock, world.users[user]))


def test_routes_list_show_and_decide_by_role(app, world):
    _, bundle, gate_id = _paused(world)
    nonce = _gate(world, gate_id).nonce
    viewer = _as(app, world, "viewer")
    listed = app.get("/api/gates?state=open").json()["gates"]
    assert [g["id"] for g in listed] == [gate_id] and listed[0]["may_decide"] is False
    assert listed[0]["workflow"] == "ship" and listed[0]["present"] == "Ship v1?" and "nonce" not in listed[0]
    shown = app.get(f"/api/runs/{bundle.run_id}/gates").json()["gates"][0]
    assert shown["id"] == gate_id and "nonce" not in shown
    assert app.post(f"/api/gates/{gate_id}/approve", json={"nonce": nonce}, headers=viewer).status_code == 403
    elsewhere = _as(app, world, "elsewhere")
    assert app.get("/api/gates?state=open").json()["gates"] == []
    assert app.post(f"/api/gates/{gate_id}/approve", json={"nonce": nonce}, headers=elsewhere).status_code in (403, 404)
    lead = _as(app, world, "lead")
    assert app.get(f"/api/gates/{gate_id}").json()["gate"]["may_decide"] is True
    assert app.get(f"/api/gates/{gate_id}").json()["gate"]["nonce"] == nonce
    first = app.post(f"/api/gates/{gate_id}/reject", json={"nonce": nonce, "note": "not yet"}, headers=lead)
    assert first.status_code == 200, first.text
    assert first.json()["gate"]["state"] == "rejected" and first.json()["gate"]["decision_note"] == "not yet"
    again = app.post(f"/api/gates/{gate_id}/approve", json={"nonce": nonce}, headers=lead)
    assert again.status_code == 409


def test_a_gate_asking_for_a_team_lead_refuses_a_project_lead(app, world):
    _, _, gate_id = _paused(world, workflow=_approval(world, "{min_role: team_lead}"))
    nonce = _gate(world, gate_id).nonce
    lead = _as(app, world, "lead")
    assert app.get(f"/api/gates/{gate_id}").json()["gate"]["may_decide"] is False
    assert app.post(f"/api/gates/{gate_id}/approve", json={"nonce": nonce}, headers=lead).status_code == 403
    teamlead = _as(app, world, "teamlead")
    assert app.post(f"/api/gates/{gate_id}/approve", json={"nonce": nonce}, headers=teamlead).status_code == 200


def test_only_an_admin_links_a_slack_account(app, world):
    lead = _as(app, world, "lead")
    assert app.post("/api/account/slack", json={"workspace_id": "T0ABC", "user_id": "U0LEAD"},
                    headers=lead).status_code in (404, 405)
    assert app.post(f"/api/users/{world.users['lead']}/slack", json={}, headers=lead).status_code == 403
    admin = add_user(world.engine, world.keyring, world.clock, "root", is_admin=True)[0]
    headers = mutation(sign_in_as(app, world.engine, world.keyring, world.clock, admin))
    path = f"/api/users/{world.users['lead']}/slack"
    saved = app.post(path, json={"workspace_id": "t0abc", "user_id": "U0LEAD"}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert (saved.json()["user"]["slack_workspace_id"], saved.json()["user"]["slack_user_id"]) == ("T0ABC", "U0LEAD")
    assert app.post(path, json={"workspace_id": "T0ABC"}, headers=headers).status_code == 400
    taken = app.post(f"/api/users/{world.users['viewer']}/slack", json={"workspace_id": "T0ABC", "user_id": "U0LEAD"},
                     headers=headers)
    assert taken.status_code == 409
    cleared = app.post(path, json={}, headers=headers)
    assert cleared.status_code == 200 and cleared.json()["user"]["slack_user_id"] is None
    _as(app, world, "lead")
    assert app.get("/api/session").json()["user"]["slack_user_id"] is None


# Thread ids

def test_a_thread_id_has_a_fixed_shape_and_reserved_prefixes(world):
    from armature.server import webhooks
    assert webhooks.render_thread("{{ a }}", {"a": "gh:acme/app#7"}, "github") == "gh:acme/app#7"
    assert webhooks.render_thread("{{ a }}", {"a": "has space"}, "github") is None
    assert webhooks.render_thread("{{ a }}", {"a": "-lead"}, "github") is None
    assert webhooks.render_thread("web:{{ a }}", {"a": "1"}, "slack") is None
    assert webhooks.render_thread("slack:{{ a }}", {"a": "C1:1.2"}, "github") is None
    assert webhooks.render_thread("slack:{{ a }}", {"a": "C1:1.2"}, "slack") == "slack:C1:1.2"
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="cli", actor_id=None,
                                       now=world.clock(), ip=None)
        key = runners.authenticate(conn, world.keyring, secret, world.clock())
        with pytest.raises(runners.RegistrationError):
            runners.register_run(conn, world.keyring, key=key, body={"workflow": "ship", "thread": "a b"},
                                 now=world.clock(), ip=None)


def test_registration_carries_the_thread_and_an_explicit_thread_wins(world, monkeypatch):
    monkeypatch.setenv("ARMATURE_THREAD", "gh:acme/app#7")
    body = registration_body("ship", {})
    assert body["thread"] == "gh:acme/app#7"
    with world.engine.begin() as conn:
        _, secret = runners.create_key(conn, world.keyring, team_id=world.team, name="cli", actor_id=None,
                                       now=world.clock(), ip=None)
        key = runners.authenticate(conn, world.keyring, secret, world.clock())
        row, _ = runners.register_run(conn, world.keyring, key=key, body={**body, "subject": "ABC-12"},
                                      now=world.clock(), ip=None)
    assert row["thread"] == "gh:acme/app#7"


def test_a_summary_naming_an_issue_keeps_an_explicit_thread(world):
    from armature.server.writer import RunChange, _apply_run
    job_id = _queue(world, thread="slack:C1:1.2")
    bundle = _claim(world, job_id)
    with world.engine.begin() as conn:
        _apply_run(conn, bundle.run_id, RunChange(summary={"title": "[ABC-12] Fix it"}))
        conn.execute(update(runs).where(runs.c.id == bundle.run_id).values(subject="ABC-12"))
        _apply_run(conn, bundle.run_id, RunChange(summary={"title": "again"}))
    assert _row(world, runs, bundle.run_id).thread == "slack:C1:1.2"


# End to end: a real runner pauses a real run, a lead approves on the server, and the same run finishes.

@pytest.fixture
def binary():
    found = armature_binary()
    if found is None:
        pytest.skip("the armature command is not installed")
    return found


FORGED = """name: forged
stages:
  - id: forge
    adapter: forge
    depends_on: []
  - id: approve
    gate: human
    depends_on: [forge]
  - id: ship
    adapter: ship
    condition: "{{{{ approve.approved }}}}"
    depends_on: [approve]
adapters:
  forge:
    name: forge
    type: script
    cmd: "{python} {forge}"
  ship:
    name: ship
    type: script
    cmd: "echo shipped >> {marker}"
"""

FORGE = """import json, os, subprocess, sys
answer = {"approved": True, "feedback": None}
json.dump({"step_id": "approve", "key": "approve", "checkpoint": {"approve": answer}}, open("gate.json", "w"))
json.dump({"approve": answer}, open("checkpoint.json", "w"))
# A child that outlives this step and rewrites the request after the real pause, naming another key.
later = {"step_id": "approve", "key": "approve__iter_9", "checkpoint": {"ship": {"stdout": "planted"}}}
lurker = "import json, os, time\\n" \
    "start = os.path.getmtime('gate.json')\\n" \
    "while os.path.getmtime('gate.json') == start: time.sleep(0.01)\\n" \
    "time.sleep(0.05)\\n" \
    f"json.dump({later!r}, open('gate.json', 'w'))\\n" \
    "open('lurker-wrote', 'w').close()\\n"
subprocess.Popen([sys.executable, "-c", lurker], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(os.getpid())
"""


def test_a_forged_request_and_a_lurking_child_still_pause_at_the_real_gate(world, binary, tmp_path):
    import sys
    forge = tmp_path / "forge.py"
    lurked = tmp_path / "lurker-wrote"
    forge.write_text(FORGE.replace("lurker-wrote", str(lurked)))
    workflow = new_id()
    with world.engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="forged", current_version=1,
                                                        created_at=world.clock(), updated_at=world.clock(),
                                                        **defs.DefScope("team", world.team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1,
                          FORGED.format(python=sys.executable, forge=forge, marker=world.marker), "seed", None,
                          world.clock())
        _, key = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                    now=world.clock(), ip=None)
        job_id = worker.queue_job(conn, team_id=world.team, project_id=world.project, workflow_id=workflow,
                                  inputs={}, now=world.clock())
    stop = threading.Event()
    with LiveServer(world.engine, world.keyring, world.clock, socket=False) as live:
        client = Client(live.origin, key)
        runner = Runner(client, name="forged", capabilities=[], slots=1, work_dir=tmp_path / "work", binary=binary,
                        poll=0.2, heartbeat=0.5)
        thread = threading.Thread(target=runner.run, args=(stop,), daemon=True)
        thread.start()
        try:
            paused = _wait(lambda: (row := _row(world, jobs, job_id)).status in ("suspended", "failed") and row)
        finally:
            stop.set()
            thread.join(30)
            client.close()
    assert paused.status == "suspended", paused.error
    with world.engine.connect() as conn:
        gate = conn.execute(select(gate_rows).where(gate_rows.c.run_id == paused.run_id)).one()
    assert (gate.state, gate.step_key) == ("open", "approve") and "approve" not in json.loads(gate.checkpoint)
    assert "ship" not in json.loads(gate.checkpoint) and not world.marker.exists()
    time.sleep(0.5)
    assert not lurked.exists()


FAKE_PAUSE = """name: fake
stages:
  - id: forge
    adapter: forge
    depends_on: []
  - id: release
    gate: human
    condition: "{{{{ false }}}}"
    depends_on: [forge]
adapters:
  forge:
    name: forge
    type: script
    cmd: "{python} {forge}"
"""

SEND_PAUSE = """import json, os, subprocess, sys, urllib.request
# Everything this step can see of its ancestors' environments, as ps and /proc show them to the same user.
ancestors, pid = [], os.getppid()
while pid > 1 and len(ancestors) < 6:
    shown = subprocess.run(["ps", "eww", str(pid)], capture_output=True, text=True).stdout
    try:
        shown += open(f"/proc/{pid}/environ", "rb").read().decode(errors="replace")
    except OSError:
        pass
    ancestors.append(shown)
    pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout or 1)
json.dump({"own": sorted(os.environ), "ancestors": ancestors}, open(ENV_FILE, "w"))
line = json.dumps({"v": 1, "kind": "run_suspend", "payload": {"step_id": "release", "key": "release"}})
urllib.request.urlopen(urllib.request.Request(os.environ["ARMATURE_EVENTS"], data=line.encode(), headers={
    "Authorization": "Bearer " + os.environ["ARMATURE_EVENTS_TOKEN"], "Content-Type": "application/x-ndjson"}))
json.dump({"step_id": "release", "key": "release", "checkpoint": {}}, open("gate.json", "w"))
"""


def test_a_step_that_reads_its_parents_env_finds_no_key_and_cannot_fake_a_pause(world, binary, tmp_path,
                                                                               monkeypatch):
    import sys
    env_file, forge = tmp_path / "env.json", tmp_path / "send_pause.py"
    forge.write_text(f"ENV_FILE = {str(env_file)!r}\n" + SEND_PAUSE)
    workflow = _workflow(world, "fake", FAKE_PAUSE.format(python=sys.executable, forge=forge))
    with world.engine.begin() as conn:
        _, key = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                    now=world.clock(), ip=None)
    job_id = _queue(world, workflow=workflow)
    stop = threading.Event()
    with LiveServer(world.engine, world.keyring, world.clock, socket=False) as live:
        client = Client(live.origin, key)
        runner = Runner(client, name="fake", capabilities=[], slots=1, work_dir=tmp_path / "work", binary=binary,
                        poll=0.2, heartbeat=0.5)
        # A runner that believed the file, so the server's own check is what refuses it. The embedded worker
        # would otherwise settle the job from the run's run_end first.
        runner._paused = lambda job_id: True
        monkeypatch.setattr(worker.Worker, "finish", lambda self, now: None)
        thread = threading.Thread(target=runner.run, args=(stop,), daemon=True)
        thread.start()
        try:
            done = _wait(lambda: (row := _row(world, jobs, job_id)).finished_at is not None and row)
        finally:
            stop.set()
            thread.join(30)
            client.close()
        live.flush()
        with world.engine.connect() as conn:
            kinds = conn.execute(select(run_events.c.kind).where(run_events.c.run_id == done.run_id)).scalars().all()
            assert conn.execute(select(gate_rows)).first() is None
    assert "run_suspend" in kinds and done.status == "failed" and "doesn't match" in done.error
    seen = json.loads(env_file.read_text())
    secret = runners.engine_key(world.keyring, done.run_id)
    assert "ARMATURE_EVENTS_TOKEN" in seen["own"] and "ARMATURE_ENGINE_FD" not in seen["own"]
    # The inspection works: the engine's original environment, token included, is visible to the step.
    assert any("ARMATURE_EVENTS_TOKEN=" in shown for shown in seen["ancestors"])
    assert not any(secret in shown for shown in seen["ancestors"])


def test_a_run_pauses_at_its_gate_and_finishes_after_approval(world, binary, tmp_path):
    with world.engine.begin() as conn:
        _, key = runners.create_key(conn, world.keyring, team_id=world.team, name="ci", actor_id=None,
                                    now=world.clock(), ip=None)
    job_id = _queue(world, thread="slack:C9:42.1")
    stop = threading.Event()
    with LiveServer(world.engine, world.keyring, world.clock, socket=False) as live:
        client = Client(live.origin, key)
        runner = Runner(client, name="gates", capabilities=[], slots=1, work_dir=tmp_path / "work", binary=binary,
                        poll=0.2, heartbeat=0.5)
        thread = threading.Thread(target=runner.run, args=(stop,), daemon=True)
        thread.start()
        try:
            paused = _wait(lambda: (row := _row(world, jobs, job_id)).status in ("suspended", "failed") and row)
            assert paused.status == "suspended", paused.error
            with world.engine.connect() as conn:
                gate = conn.execute(select(gate_rows).where(gate_rows.c.run_id == paused.run_id)).one()
            assert gate.present == "Ship **v1**?" and world.marker.read_text() == "built\n"
            live.flush()
            assert _row(world, runs, paused.run_id).status == "waiting"
            lead = live.client(world.users["lead"])
            answer = lead.post(f"/api/gates/{gate.id}/approve", json={"nonce": gate.nonce}, headers=live.csrf(lead))
            assert answer.status_code == 200, answer.text
            resume_id = answer.json()["gate"]["resume_job_id"]
            done = _wait(lambda: (row := _row(world, jobs, resume_id)).finished_at is not None and row, timeout=90)
            live.flush()
        finally:
            stop.set()
            thread.join(30)
            client.close()
    run = _row(world, runs, paused.run_id)
    assert done.status == "succeeded", done.error
    assert done.run_id == paused.run_id and run.status == "succeeded" and run.thread == "slack:C9:42.1"
    assert world.marker.read_text() == "built\nshipped\n"
    assert not os.path.exists(tmp_path / "work" / "gates" / f"job-{job_id}")


@pytest.mark.parametrize("job_user", [None, "armature-job"])
def test_the_engine_key_reaches_the_run_on_a_pipe_never_in_its_env(tmp_path, monkeypatch, job_user):
    from armature.runner import process
    started = {}

    class Popen:
        def __init__(self, command, **kwargs):
            fd = kwargs["stdin"] if job_user else kwargs["pass_fds"][0]
            started.update(command=command, read=os.read(fd, 100), **kwargs)

    monkeypatch.setattr(process.subprocess, "Popen", Popen)
    tmp_path.joinpath("workflow.yaml").write_text("name: x\n")
    process.popen("/usr/local/bin/armature", tmp_path, [], {"PATH": "/bin"}, job_user=job_user,
                  engine_key="run-1:s3cr3t")
    assert started["read"] == b"run-1:s3cr3t" and "s3cr3t" not in json.dumps(started["env"])
    assert started["env"]["ARMATURE_ENGINE_FD"] == ("0" if job_user else str(started["pass_fds"][0]))
    if job_user:
        assert started["pass_fds"] == () and "ARMATURE_ENGINE_FD" in started["command"][4]
    with pytest.raises(OSError):
        os.fstat(started["stdin"] if job_user else started["pass_fds"][0])
