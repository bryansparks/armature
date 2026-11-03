"""Deleting runs from the history: everything a run owns goes, links from other records are cleared, open runs stay."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select

from armature.server import defs, history
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    conversations,
    findings,
    gates,
    jobs,
    metadata,
    operations,
    review_marks,
    run_artifacts,
    run_events,
    run_steps,
    run_tokens,
    runs,
    tool_calls,
    trigger_seen,
    triggers,
)
from tests.server.app_support import add_user
from tests.server.live_support import add_team

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(sqlite_engine, keyring):
    team, project = add_team(sqlite_engine, lambda: NOW, "mobile")
    admin, _ = add_user(sqlite_engine, keyring, lambda: NOW, "root", is_admin=True)
    workflow, trigger = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="pr-review", current_version=1,
                                                        created_at=NOW, updated_at=NOW,
                                                        **defs.DefScope("team", team).columns()))
        conn.execute(insert(triggers).values(id=trigger, team_id=team, workflow_id=workflow, kind="webhook",
                                             created_at=NOW))
    return {"team": team, "project": project, "admin": admin, "workflow": workflow, "trigger": trigger}


def _run(conn, world, status="succeeded", **values):
    run_id = new_id()
    conn.execute(insert(runs).values(id=run_id, team_id=world["team"], workflow_id=world["workflow"],
                                     workflow_name="pr-review", status=status, started_at=NOW, **values))
    return run_id


def _job(conn, world, run_id, status="succeeded", **values):
    job_id = new_id()
    conn.execute(insert(jobs).values(id=job_id, team_id=world["team"], workflow_id=world["workflow"], run_id=run_id,
                                     status=status, created_at=NOW, **values))
    return job_id


def _everything(conn, world, run_id, job_id):
    """One row in each table that belongs to a run, and a gate the run opened."""
    team = world["team"]
    conn.execute(insert(run_events).values(run_id=run_id, seq=1, ts=NOW, kind="step_start"))
    conn.execute(insert(run_steps).values(run_id=run_id, step_key="a", step_id="a", status="succeeded"))
    conn.execute(insert(run_artifacts).values(run_id=run_id, name="notes.md", media_type="text/markdown",
                                              content="x", size=1, created_at=NOW))
    conn.execute(insert(run_tokens).values(token_hmac=new_id(), run_id=run_id, team_id=team,
                                           expires_at=NOW + timedelta(hours=1)))
    conn.execute(insert(findings).values(id=new_id(), run_id=run_id, finding_key="f1"))
    conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=team, step_id="a", tool="slack.post",
                                           mode="live", outcome="ok", created_at=NOW))
    conn.execute(insert(operations).values(id=new_id(), action_id="act", run_id=run_id, started_at=NOW))
    conn.execute(insert(review_marks).values(id=new_id(), team_id=team, project_id=world["project"], repo="a/b",
                                             head_sha=run_id.replace("-", "")[:40].ljust(40, "0"), run_id=run_id,
                                             conclusion="success", created_at=NOW))
    gate = new_id()
    conn.execute(insert(gates).values(id=gate, run_id=run_id, team_id=team, job_id=job_id, step_id="ok",
                                      step_key="ok", nonce=new_id(), state="approved", created_at=NOW,
                                      expires_at=NOW + timedelta(hours=1)))
    return gate


def _count(conn, table, *where):
    return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def test_every_table_with_a_run_or_job_column_is_covered():
    covered = {(t.name, c.name) for target in ("runs", "jobs", "gates") for t, c in history.references(target)}
    for table in metadata.sorted_tables:
        for column in table.columns:
            if column.name in ("run_id", "job_id", "rerun_of") or column.name.endswith("_job_id"):
                assert (table.name, column.name) in covered, (table.name, column.name)


def test_a_run_goes_with_its_jobs_and_rows_and_other_records_only_lose_their_link(sqlite_engine, world):
    with sqlite_engine.begin() as conn:
        target = _run(conn, world)
        job = _job(conn, world, target)
        gate = _everything(conn, world, target, job)
        rerun = _run(conn, world, rerun_of=target)
        _everything(conn, world, rerun, _job(conn, world, rerun, rerun_of=target))
        resumed = _job(conn, world, rerun, resume_of_gate=gate)
        conversation = new_id()
        conn.execute(insert(conversations).values(id=conversation, team_id=world["team"], workflow_id=world["workflow"],
                                                  kind="web", thread="t1", active_job_id=job, created_at=NOW,
                                                  updated_at=NOW))
        conn.execute(insert(trigger_seen).values(trigger_id=world["trigger"], item_key="pr-7", outcome="queued",
                                                 job_id=job, first_seen_at=NOW, last_seen_at=NOW))
        [plan] = history.delete_runs(conn, [target], actor_id=world["admin"])
    assert plan.job_ids == [job]
    assert plan.deleted == {"run_events": 1, "run_steps": 1, "run_artifacts": 1, "run_tokens": 1, "findings": 1,
                            "tool_calls": 1, "operations": 1, "review_marks": 1, "gates": 1, "jobs": 1, "runs": 1}
    assert plan.cleared == {"runs.rerun_of": 1, "jobs.rerun_of": 1, "jobs.resume_of_gate": 1,
                            "conversations.active_job_id": 1, "trigger_seen.job_id": 1}
    with sqlite_engine.connect() as conn:
        for table in (run_events, run_steps, run_artifacts, run_tokens, findings, tool_calls, operations,
                      review_marks, gates):
            assert _count(conn, table, table.c.run_id == target) == 0
            assert _count(conn, table, table.c.run_id == rerun) == 1
        assert _count(conn, runs, runs.c.id == target) == 0 and _count(conn, jobs, jobs.c.id == job) == 0
        assert conn.execute(select(runs.c.rerun_of).where(runs.c.id == rerun)).scalar() is None
        assert _count(conn, jobs, jobs.c.run_id == rerun, jobs.c.rerun_of.is_(None)) == 2
        assert conn.execute(select(jobs.c.resume_of_gate).where(jobs.c.id == resumed)).scalar() is None
        assert conn.execute(select(conversations.c.active_job_id)
                            .where(conversations.c.id == conversation)).scalar() is None
        assert conn.execute(select(trigger_seen.c.job_id)).scalar() is None
        entry = conn.execute(select(audit).where(audit.c.action == "run.delete")).one()
    assert entry.actor_id == world["admin"] and entry.target_id == target and entry.team_id == world["team"]
    assert entry.detail["workflow"] == "pr-review" and entry.detail["jobs"] == [job]


@pytest.mark.parametrize(("run_status", "job_status"), [("running", "running"), ("queued", None),
                                                        ("waiting", "succeeded"), ("lost", "running")])
def test_an_open_run_is_refused_and_nothing_changes(sqlite_engine, world, run_status, job_status):
    with sqlite_engine.begin() as conn:
        ended = _run(conn, world)
        _job(conn, world, ended)
        opened = _run(conn, world, status=run_status)
        if job_status:
            _job(conn, world, opened, status=job_status)
    with sqlite_engine.begin() as conn:
        with pytest.raises(history.HistoryError, match=f"Run {opened} is still open"):
            history.delete_runs(conn, [ended, opened], actor_id=world["admin"])
    with sqlite_engine.connect() as conn:
        assert _count(conn, runs) == 2 and _count(conn, jobs) == (2 if job_status else 1)


def test_a_lost_run_whose_job_ended_can_go(sqlite_engine, world):
    with sqlite_engine.begin() as conn:
        lost = _run(conn, world, status="lost")
        _job(conn, world, lost, status="lost")
        history.delete_runs(conn, [lost], actor_id=world["admin"])
    with sqlite_engine.connect() as conn:
        assert _count(conn, runs) == 0


def test_a_dry_run_counts_and_changes_nothing(sqlite_engine, world):
    with sqlite_engine.begin() as conn:
        target = _run(conn, world)
        _everything(conn, world, target, _job(conn, world, target))
        [plan] = history.delete_runs(conn, [target], actor_id=world["admin"], dry_run=True)
    assert plan.deleted["run_events"] == 1 and plan.deleted["runs"] == 1
    with sqlite_engine.connect() as conn:
        assert _count(conn, runs) == 1 and _count(conn, run_events) == 1 and _count(conn, gates) == 1
        assert _count(conn, audit, audit.c.action == "run.delete") == 0


def test_an_unknown_run_is_refused(sqlite_engine, world):
    with sqlite_engine.begin() as conn:
        with pytest.raises(history.HistoryError, match="No run has the id nope"):
            history.delete_runs(conn, ["nope"], actor_id=world["admin"])
