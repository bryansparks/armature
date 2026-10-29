"""Where runs executed: the shared location rules and the backfill of recorded runs."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import event, insert, select

from armature.events import location
from armature.server.db import new_id, utcnow
from armature.server.db.schema import (
    aws_connections,
    environment_types,
    environments,
    jobs,
    run_steps,
    runner_keys,
    runners,
    runs,
    teams,
    workflows,
)
from armature.server.locations import backfill

M1 = {"kind": "local", "label": "M1 (local)", "chip": "Apple M1"}


def test_a_location_is_rebuilt_from_known_fields_with_the_servers_label():
    assert location.parse({"kind": "local", "chip": "Apple M2 Max", "label": "spoofed", "extra": 1}) == {
        "kind": "local", "label": "M2 Max (local)", "chip": "Apple M2 Max"}
    assert location.parse({"kind": "local", "machine": "x86_64"})["label"] == "x86_64 (local)"
    assert location.parse({"kind": "local"})["label"] == "Local"
    assert location.parse({"kind": "aws-mac", "instance": "i-0\n1"}) == {"kind": "aws-mac", "label": "AWS macOS",
                                                                          "instance": "i-01"}
    for bad in (None, "aws-mac", {"kind": "gcp"}, {"label": "AWS Linux"}):
        assert location.parse(bad) is None


def test_this_machine_never_names_its_host():
    assert "host" not in location.this_machine()
    assert location.parse({"kind": "local", "chip": "Apple M1", "host": "stephans-laptop"}) == {
        "kind": "local", "label": "M1 (local)", "chip": "Apple M1"}


def test_a_same_user_runner_says_so_in_its_label():
    assert location.parse({"kind": "local", "chip": "Apple M1", "same_user": True}, trusted=True) == {
        "kind": "local", "label": "M1 (local, same user)", "chip": "Apple M1", "same_user": True}
    assert location.parse({"kind": "local", "chip": "Apple M1", "same_user": True}) == M1
    assert location.parse({"kind": "local", "same_user": "yes"}, trusted=True) == {"kind": "local", "label": "Local"}
    assert location.parse({"kind": "aws-mac", "same_user": True}, trusted=True) == {"kind": "aws-mac",
                                                                                     "label": "AWS macOS"}
    assert location.this_machine(same_user=True)["same_user"] is True


def test_ops_placements_map_to_locations():
    assert location.from_placement({"where": "m1", "fell_back": True}) == M1
    assert location.from_placement({"where": "aws-spot", "instance_type": "c7i.xlarge"})["kind"] == "aws-linux"
    assert location.from_placement({"where": "aws-mac", "host": "i-0mac"})["instance"] == "i-0mac"
    assert location.from_placement({"where": "aws-session", "launched": False}) is None
    assert location.from_placement({"where": "somewhere"}) is None
    assert location.from_event("log", {"message": "building a/b#1 on aws-spot: make"})["kind"] == "aws-linux"
    assert location.from_event("log", {"message": "a build on aws-mac"}) is None
    assert location.better(M1, location.make("aws-mac")) and not location.better(location.make("aws-mac"), M1)
    assert not location.better(location.make("aws-mac"), location.make("aws-linux"))


def _run(conn, team, **values):
    run_id = new_id()
    conn.execute(insert(runs).values(id=run_id, team_id=team, status="succeeded", started_at=utcnow(), **values))
    return run_id


def _job(conn, team, workflow, run_id, **values):
    job_id = new_id()
    conn.execute(insert(jobs).values(id=job_id, team_id=team, workflow_id=workflow, run_id=run_id, status="succeeded",
                                     attempts=1, **{"created_at": utcnow(), **values}))
    return job_id


def test_backfill_locates_runs_from_jobs_and_step_outputs_once(sqlite_engine):
    team, workflow, key, runner, connection, kind = (new_id() for _ in range(6))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="mobile", created_at=utcnow()))
        conn.execute(insert(workflows).values(id=workflow, team_id=team, name="review", current_version=1,
                                              created_at=utcnow(), updated_at=utcnow()))
        conn.execute(insert(runner_keys).values(id=key, team_id=team, name="m1", key_hmac="h" * 64,
                                                created_at=utcnow()))
        conn.execute(insert(runners).values(id=runner, runner_key_id=key, name="m1", state="online",
                                            created_at=utcnow(), last_seen_at=utcnow(),
                                            platform={"kind": "local", "chip": "Apple M1"}))
        conn.execute(insert(aws_connections).values(
            id=connection, name="default", region="us-east-1", auth_kind="access_key",
            tag_project="armature-platform", install_id="install-1", allow_host_allocation=False, max_concurrent=4,
            max_daily_launches=50, boot_timeout_s=420, max_lifetime_s=5400, state="ok", created_at=utcnow(),
            updated_at=utcnow()))
        conn.execute(insert(environment_types).values(
            id=kind, name="android", kind="ec2-linux", image="ami-0123456789abcdef0", market="on-demand",
            job_timeout_s=3600, enabled=True, created_at=utcnow(), updated_at=utcnow()))
        in_env = _run(conn, team)
        env_job = _job(conn, team, workflow, in_env, claimed_by="runner:x")
        conn.execute(insert(environments).values(
            id=new_id(), connection_id=connection, type_id=kind, purpose="job", job_id=env_job, provider_id="i-0abc",
            instance_type="m6i.large", state="terminated", requested_at=utcnow()))
        on_runner = _run(conn, team)
        _job(conn, team, workflow, on_runner, runner_id=runner, claimed_by=f"runner:{runner}")
        embedded = _run(conn, team)
        _job(conn, team, workflow, embedded, claimed_by="m1.local:4242:0a1b2c3d")
        env_claimed = _run(conn, team)
        _job(conn, team, workflow, env_claimed, claimed_by="env:0f6c")
        rerun = _run(conn, team)
        _job(conn, team, workflow, rerun, runner_id=runner, claimed_by=f"runner:{runner}",
             created_at=utcnow() - timedelta(hours=1))
        _job(conn, team, workflow, rerun, claimed_by="m1.local:4242:0a1b2c3d")
        ops_mac = _run(conn, team)
        conn.execute(insert(run_steps).values(run_id=ops_mac, step_key="validate", step_id="validate",
                                              status="succeeded",
                                              output={"placement": {"where": "aws-mac", "host": "i-0mac"}}))
        ops_fan = _run(conn, team)
        conn.execute(insert(run_steps).values(run_id=ops_fan, step_key="build", step_id="build", status="succeeded",
                                              output=[{"placement": {"where": "m1"}},
                                                      {"placement": {"where": "aws-spot"}}, None]))
        silent = _run(conn, team)
        conn.execute(insert(run_steps).values(run_id=silent, step_key="s", step_id="s", status="succeeded",
                                              output={"content": "no placement"}))
        stated = _run(conn, team, location=M1)

    job_reads = []

    def count(_conn, _cursor, statement, *_):
        if "FROM jobs" in statement:
            job_reads.append(statement)

    event.listen(sqlite_engine, "before_cursor_execute", count)
    assert backfill(sqlite_engine, batch=2) == {"aws-linux": 2, "aws-mac": 1, "local": 3, "unknown": 2}
    event.remove(sqlite_engine, "before_cursor_execute", count)
    assert len(job_reads) == 4, "one jobs query per batch of two"
    with sqlite_engine.connect() as conn:
        found = dict(conn.execute(select(runs.c.id, runs.c.location)).all())
    assert found[in_env] == {"kind": "aws-linux", "label": "AWS Linux", "instance": "i-0abc",
                             "environment_type": "android", "instance_type": "m6i.large"}
    assert found[on_runner] == M1 and found[stated] == M1
    assert found[embedded] == found[rerun] == {"kind": "local", "label": "Local"}
    assert found[env_claimed] == {"kind": "unknown", "label": "Unknown"}
    assert found[ops_mac] == {"kind": "aws-mac", "label": "AWS macOS", "instance": "i-0mac"}
    assert found[ops_fan]["kind"] == "aws-linux"
    assert found[silent] == {"kind": "unknown", "label": "Unknown"}
    assert backfill(sqlite_engine) == {}
    with sqlite_engine.connect() as conn:
        assert dict(conn.execute(select(runs.c.id, runs.c.location)).all()) == found
