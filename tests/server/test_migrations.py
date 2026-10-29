import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError

from armature.server import secrets
from armature.server.db import make_engine, migrate, new_id, utcnow
from armature.server.db.schema import (
    documents,
    metadata,
    run_events,
    runs,
    teams,
    webhook_deliveries,
    webhooks,
    workflows,
)
from armature.server.db.schema import secrets as secrets_table

# pymysql reports a failed CHECK (MySQL error 3819) as OperationalError.
CHECK_FAILED = (IntegrityError, OperationalError)


def test_upgrade_creates_every_table(engine):
    names = set(inspect(engine).get_table_names())
    assert names == set(metadata.tables) | {"alembic_version"}


def test_migration_matches_metadata(engine):
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diffs == []


def test_upgrade_is_idempotent(db_url, engine):
    migrate(db_url)
    assert "teams" in inspect(engine).get_table_names()


def test_index_layout(engine):
    insp = inspect(engine)
    run_indexes = {tuple(ix["column_names"]) for ix in insp.get_indexes("runs")}
    assert ("team_id", "started_at") in run_indexes
    assert insp.get_pk_constraint("run_events")["constrained_columns"] == ["run_id", "seq"]
    assert insp.get_indexes("run_events") == []


def test_rows_round_trip(engine, keyring):
    team_id, run_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=utcnow()))
        conn.execute(insert(runs).values(id=run_id, team_id=team_id, status="running", started_at=utcnow(),
                                         summary={"roles": ["security"], "ok": True}))
        conn.execute(insert(run_events).values(run_id=run_id, seq=1, ts=utcnow(), kind="step",
                                               payload={"text": "héllo"}))
        secrets.set_secret(conn, keyring, team_id=team_id, name="GH_TOKEN", value="t0k", actor_id=None)
        secrets.set_secret(conn, keyring, team_id=None, name="GH_TOKEN", value="instance", actor_id=None)
    with engine.connect() as conn:
        assert conn.execute(select(runs.c.summary)).scalar_one() == {"roles": ["security"], "ok": True}
        assert conn.execute(select(run_events.c.payload)).scalar_one() == {"text": "héllo"}
        assert secrets.resolve_for_run(conn, keyring, team_id, ["GH_TOKEN"]) == {"GH_TOKEN": "t0k"}


def test_downgrade_to_base(db_url, engine):
    from alembic import command

    from armature.server.db import alembic_config
    command.downgrade(alembic_config(db_url), "base")
    fresh = make_engine(db_url)
    try:
        assert set(inspect(fresh).get_table_names()) <= {"alembic_version"}
    finally:
        fresh.dispose()


def _secret_row(name, team_id=None, scope="*"):
    return {"id": new_id(), "team_id": team_id, "scope": scope, "name": name, "ciphertext": "x", "key_id": "abcd1234",
            "updated_at": utcnow()}


def test_two_instance_secrets_with_one_name_violate_unique(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(secrets_table).values(_secret_row("GH_TOKEN")))
        conn.execute(insert(secrets_table).values(_secret_row("GH_TOKEN")))


def test_two_instance_documents_with_one_key_violate_unique(engine):
    def row():
        return {"id": new_id(), "team_id": None, "scope": "*", "kind": "role", "key": "security",
                "current_version": 1, "created_at": utcnow(), "updated_at": utcnow()}
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(documents).values(row()))
        conn.execute(insert(documents).values(row()))


def test_timestamps_round_trip_as_aware_utc(engine):
    team_id = new_id()
    written = datetime(2026, 10, 23, 9, 30, 15, 123456, tzinfo=timezone(timedelta(hours=-6)))
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name="tz", created_at=written))
    with engine.connect() as conn:
        read = conn.execute(select(teams.c.created_at).where(teams.c.id == team_id)).scalar_one()
    assert read.tzinfo is not None and read.utcoffset() == timedelta(0)
    assert read == written
    assert read == datetime(2026, 10, 23, 15, 30, 15, 123456, tzinfo=UTC)


def test_naive_timestamp_refused(engine):
    with pytest.raises(StatementError, match="naive"), engine.begin() as conn:
        conn.execute(insert(teams).values(id=new_id(), name="naive", created_at=datetime(2026, 10, 23)))


def test_scope_must_match_team(engine):
    team_a, team_b = new_id(), new_id()
    with engine.begin() as conn:
        for name, team_id in (("a", team_a), ("b", team_b)):
            conn.execute(insert(teams).values(id=team_id, name=name, created_at=utcnow()))
        conn.execute(insert(secrets_table).values(_secret_row("K", team_id=team_a, scope=team_a)))
    bad_rows = [
        _secret_row("X", team_id=team_a, scope=team_b),
        _secret_row("Y", team_id=team_a, scope="*"),
        _secret_row("Z", team_id=None, scope=team_a),
    ]
    for row in bad_rows:
        with pytest.raises(CHECK_FAILED), engine.begin() as conn:
            conn.execute(insert(secrets_table).values(row))
    with pytest.raises(CHECK_FAILED), engine.begin() as conn:
        conn.execute(update(secrets_table).where(secrets_table.c.team_id == team_a).values(scope=team_b))
    with pytest.raises(CHECK_FAILED), engine.begin() as conn:
        conn.execute(insert(documents).values(id=new_id(), team_id=None, scope=team_a, kind="role", key="k",
                                              current_version=1, created_at=utcnow(), updated_at=utcnow()))


def test_membership_source_defaults_to_manual(engine):
    from armature.server.db.schema import memberships, users
    user_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(users).values(id=user_id, username="u", last_totp_step=0, is_admin=False, disabled=False,
                                          created_at=utcnow()))
        conn.execute(insert(memberships).values(id=new_id(), user_id=user_id, scope_kind="team", scope_id=new_id(),
                                                role="viewer", created_at=utcnow()))
        assert conn.execute(select(memberships.c.source)).scalar_one() == "manual"
    with pytest.raises(CHECK_FAILED), engine.begin() as conn:
        conn.execute(insert(memberships).values(id=new_id(), user_id=user_id, scope_kind="team", scope_id=new_id(),
                                                role="viewer", source="ldap", created_at=utcnow()))


def test_0003_upgrades_a_live_database_and_backfills_scope(db_url):
    migrate(db_url, "0002")
    team_id, workflow_id = new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            stamp = {"at": "2026-10-23 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'live', :at)"),
                         {"t": team_id, **stamp})
            conn.execute(text("INSERT INTO workflows (id, team_id, name, current_version, created_at, updated_at) "
                              "VALUES (:w, :t, 'wf', 1, :at, :at)"), {"w": workflow_id, "t": team_id, **stamp})
            conn.execute(text("INSERT INTO runs (id, team_id, workflow_id, status, started_at) "
                              "VALUES (:r, :t, :w, 'succeeded', :at)"),
                         {"r": new_id(), "t": team_id, "w": workflow_id, **stamp})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(select(workflows.c.scope).where(workflows.c.id == workflow_id)).scalar() == team_id
    finally:
        engine.dispose()


def test_0005_backfills_runner_key_scope_and_allows_instance_keys(db_url):
    from armature.server.db.schema import runner_keys
    migrate(db_url, "0004")
    team_id, key_id = new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            stamp = {"at": "2026-10-23 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'live', :at)"),
                         {"t": team_id, **stamp})
            conn.execute(text("INSERT INTO runner_keys (id, team_id, name, key_hmac, created_at) "
                              "VALUES (:k, :t, 'ci', 'h1', :at)"), {"k": key_id, "t": team_id, **stamp})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            assert conn.execute(select(runner_keys.c.scope).where(runner_keys.c.id == key_id)).scalar() == team_id
            conn.execute(insert(runner_keys).values(id=new_id(), team_id=None, name="host", key_hmac="h2",
                                                    created_at=utcnow()))
            assert conn.execute(select(runner_keys.c.scope).where(runner_keys.c.key_hmac == "h2")).scalar() == "*"
        with pytest.raises(CHECK_FAILED), engine.begin() as conn:
            conn.execute(insert(runner_keys).values(id=new_id(), team_id=None, scope=team_id, name="bad",
                                                    key_hmac="h3", created_at=utcnow()))
    finally:
        engine.dispose()


def test_0009_backfills_kind_and_links_runs_by_scope_precedence(db_url):
    migrate(db_url, "0008")
    own, other, project = new_id(), new_id(), new_id()
    frozen = {"code-review": "review", "pr-role-review": "review", "pr-review": "review", "pr-fix": "fix",
              "pr-merged": "merge", "ios-port": "port", "dev-work": "work", "jira-work": "work", "Code-Review": "other"}
    defined = [(f"instance:{name}", None, None, "*", name) for name in frozen]
    defined += [("team_review", own, None, own, "code-review"), ("project_review", own, project, project, "code-review"),
                ("team_only", own, None, own, "team-only"), ("team_build", own, None, own, "build")]
    ids = {key: new_id() for key, *_ in defined}
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-27 12:00:00"}
            for team_id, name in ((own, "own"), (other, "other")):
                conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, :n, :at)"),
                             {"t": team_id, "n": name, **at})
            conn.execute(text("INSERT INTO projects (id, team_id, name, created_at) VALUES (:p, :t, 'app', :at)"),
                         {"p": project, "t": own, **at})
            for key, team_id, project_id, scope, name in defined:
                conn.execute(text("INSERT INTO workflows (id, team_id, project_id, scope, name, current_version, "
                                  "created_at, updated_at) VALUES (:w, :t, :p, :s, :n, 1, :at, :at)"),
                             {"w": ids[key], "t": team_id, "p": project_id, "s": scope, "n": name, **at})
            runs_by_key = {}
            for key, team_id, project_id, name in (
                    ("in_project", own, project, "code-review"),
                    ("in_team", own, None, "code-review"),
                    ("other_team_review", other, None, "code-review"),
                    ("other_team_only", other, None, "team-only"),
                    ("project_build", own, project, "build"),
                    ("cased", own, None, "CODE-REVIEW"),
                    ("unknown", own, None, "gone")):
                runs_by_key[key] = new_id()
                conn.execute(text("INSERT INTO runs (id, team_id, project_id, workflow_name, status, started_at) "
                                  "VALUES (:r, :t, :p, :n, 'succeeded', :at)"),
                             {"r": runs_by_key[key], "t": team_id, "p": project_id, "n": name, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            kinds = dict(conn.execute(select(workflows.c.id, workflows.c.kind)).all())
            linked = dict(conn.execute(select(runs.c.id, runs.c.workflow_id)).all())
    finally:
        engine.dispose()
    assert {name: kinds[ids[f"instance:{name}"]] for name in frozen} == frozen
    assert (kinds[ids["team_review"]], kinds[ids["team_only"]]) == ("review", "other")
    assert {key: linked[run_id] for key, run_id in runs_by_key.items()} == {
        "in_project": ids["project_review"], "in_team": ids["team_review"],
        "other_team_review": ids["instance:code-review"], "other_team_only": None,
        "project_build": ids["team_build"], "cased": None, "unknown": None}


def test_0011_backfills_threads_from_subjects_titles_and_pull_request_links(db_url):
    migrate(db_url, "0010")
    own, other = new_id(), new_id()
    pull_link = json.dumps({"links": [{"label": "Pull request", "url": "https://github.com/acme/app/pull/7"}]})
    seeded = [("dev", own, "MEM-1", pull_link), ("review", own, "acme/app#7", None),
              ("fix", own, "acme/app#7", json.dumps({"title": "Fix comments"})),
              ("titled", own, "acme/app#8", json.dumps({"title": "[MEM-2] Tidy"})),
              ("titled_sibling", own, "acme/app#8", json.dumps({"title": "Follow up"})),
              ("too_long", own, "acme/app#10", json.dumps({"title": f"[{'A' * 250}-1] Long"})),
              ("lower", own, "acme/app#9", json.dumps({"title": "[mem-3] Tidy"})),
              ("other_team", other, "acme/app#7", None), ("loose", own, None, None), ("digest", own, "digest", None)]
    ids = {name: new_id() for name, *_ in seeded}
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-27 12:00:00"}
            for team_id, name in ((own, "own"), (other, "other")):
                conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, :n, :at)"),
                             {"t": team_id, "n": name, **at})
            for name, team_id, subject, summary in seeded:
                conn.execute(text("INSERT INTO runs (id, team_id, status, started_at, subject, summary) "
                                  "VALUES (:r, :t, 'succeeded', :at, :s, :m)"),
                             {"r": ids[name], "t": team_id, "s": subject, "m": summary, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            threads = dict(conn.execute(select(runs.c.id, runs.c.thread)).all())
    finally:
        engine.dispose()
    assert {name: threads[run_id] for name, run_id in ids.items()} == {
        "dev": "MEM-1", "review": "MEM-1", "fix": "MEM-1", "titled": "MEM-2", "titled_sibling": "MEM-2",
        "too_long": None, "lower": None, "other_team": None,
        "loose": None, "digest": None}


def test_0012_approves_existing_webhooks_with_pinning_off(db_url):
    migrate(db_url, "0011")
    team, hook, delivery, later = new_id(), new_id(), new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-27 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            conn.execute(text("INSERT INTO webhooks (id, team_id, name, source, path_token, secret_name, enabled, "
                              "created_at, updated_at) VALUES (:h, :t, 'gh', 'github', 'tok', 'S', :on, :at, :at)"),
                         {"h": hook, "t": team, "on": True, **at})
            conn.execute(text("INSERT INTO webhook_deliveries (id, webhook_id, received_at, status) "
                              "VALUES (:d, :h, :at, 'accepted')"), {"d": delivery, "h": hook, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(webhooks).where(webhooks.c.id == hook)).first()
            kept = conn.execute(select(webhook_deliveries.c.status, webhook_deliveries.c.sender)).all()
            conn.execute(update(webhook_deliveries).values(status="held"))
            conn.execute(text("INSERT INTO webhooks (id, team_id, name, source, path_token, secret_name, enabled, "
                              "created_at, updated_at) VALUES (:h, :t, 'new', 'jira', 'tok2', 'S', :on, :at, :at)"),
                         {"h": later, "t": team, "on": True, "at": "2026-10-28 12:00:00"})
            fresh = conn.execute(select(webhooks.c.approval).where(webhooks.c.id == later)).scalar()
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(update(webhooks).values(approval="maybe"))
    finally:
        engine.dispose()
    assert (row.approval, row.pinned_senders, row.enabled) == ("approved", None, True)
    assert fresh == "pending"
    assert [tuple(r) for r in kept] == [("accepted", None)]


def test_0012_downgrade_drops_new_sources_disables_pending_and_unholds(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    migrate(db_url)
    team, ids = new_id(), {n: new_id() for n in ("github", "jira", "pending")}
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-27 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            for name, source, approval in (("github", "github", "approved"), ("jira", "jira", "approved"),
                                           ("pending", "github", "pending")):
                conn.execute(text("INSERT INTO webhooks (id, team_id, name, source, path_token, secret_name, enabled, "
                                  "approval, created_at, updated_at) VALUES (:h, :t, :n, :s, :k, 'S', :on, :a, :at, :at)"),
                             {"h": ids[name], "t": team, "n": name, "s": source, "k": f"tok-{name}", "on": True,
                              "a": approval, **at})
            for name in ("github", "jira"):
                conn.execute(text("INSERT INTO webhook_deliveries (id, webhook_id, received_at, status, sender) "
                                  "VALUES (:d, :h, :at, 'held', 'x')"), {"d": new_id(), "h": ids[name], **at})
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0011")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = {r.name: r for r in conn.execute(text("SELECT name, enabled FROM webhooks")).all()}
            statuses = [r[0] for r in conn.execute(text("SELECT status FROM webhook_deliveries")).all()]
        assert "approval" not in {c["name"] for c in inspect(engine).get_columns("webhooks")}
    finally:
        engine.dispose()
    assert set(rows) == {"github", "pending"} and bool(rows["github"].enabled) and not rows["pending"].enabled
    assert statuses == ["ignored"]


def test_0013_keeps_live_jobs_claimable_and_keys_standing(db_url):
    from alembic import command

    from armature.server import worker
    from armature.server.db import alembic_config
    from armature.server.db.schema import jobs, runner_keys
    migrate(db_url, "0012")
    team, workflow, key, queued, running = new_id(), new_id(), new_id(), new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-27 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            conn.execute(text("INSERT INTO workflows (id, team_id, scope, name, kind, current_version, created_at, "
                              "updated_at) VALUES (:w, :t, :t, 'review', 'review', 1, :at, :at)"),
                         {"w": workflow, "t": team, **at})
            conn.execute(text("INSERT INTO runner_keys (id, team_id, scope, name, key_hmac, created_at) "
                              "VALUES (:k, :t, :t, 'ci', 'h1', :at)"), {"k": key, "t": team, **at})
            conn.execute(text("INSERT INTO runs (id, team_id, workflow_id, status, runner_key_id, started_at) "
                              "VALUES (:r, :t, :w, 'running', :k, :at)"), {"r": new_id(), "t": team, "w": workflow,
                                                                          "k": key, **at})
            for job_id, status in ((queued, "queued"), (running, "running")):
                conn.execute(text("INSERT INTO jobs (id, team_id, workflow_id, status, attempts, claimed_by, "
                                  "lease_expires_at, created_at) VALUES (:j, :t, :w, :s, 1, 'host:1', :at, :at)"),
                             {"j": job_id, "t": team, "w": workflow, "s": status, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            rows = {r.id: r for r in conn.execute(select(jobs)).all()}
            kind = conn.execute(select(runner_keys.c.kind, runner_keys.c.capabilities)).one()
            found = worker.claimable(conn, capabilities=[], team_id=team)
            conn.execute(update(jobs).where(jobs.c.id == queued).values(status="canceled"))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(update(runner_keys).values(kind="borrowed"))
    finally:
        engine.dispose()
    assert {(r.kind, r.requires, r.runner_id, r.cancel_requested_at) for r in rows.values()} == {
        ("workflow", None, None, None)}
    assert rows[running].status == "running" and rows[running].claimed_by == "host:1"
    assert tuple(kind) == ("standing", None)
    assert found == [queued]
    command.downgrade(alembic_config(db_url), "0012")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            statuses = dict(conn.execute(text("SELECT id, status FROM jobs")).all())
        assert "runners" not in inspect(engine).get_table_names()
        assert "kind" not in {c["name"] for c in inspect(engine).get_columns("runner_keys")}
    finally:
        engine.dispose()
    assert statuses == {queued: "failed", running: "running"}


def test_0014_adds_environments_and_downgrade_fails_waiting_jobs(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import aws_connections, environments, jobs, projects
    migrate(db_url, "0013")
    team, project, workflow, job = new_id(), new_id(), new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-29 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            conn.execute(text("INSERT INTO projects (id, team_id, name, created_at) VALUES (:p, :t, 'app', :at)"),
                         {"p": project, "t": team, **at})
            conn.execute(text("INSERT INTO workflows (id, team_id, scope, name, kind, current_version, created_at, "
                              "updated_at) VALUES (:w, :t, :t, 'build', 'other', 1, :at, :at)"),
                         {"w": workflow, "t": team, **at})
            conn.execute(text("INSERT INTO jobs (id, team_id, project_id, workflow_id, status, attempts, created_at) "
                              "VALUES (:j, :t, :p, :w, 'queued', 0, :at)"),
                         {"j": job, "t": team, "p": project, "w": workflow, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    connection = {"id": new_id(), "name": "default", "region": "us-east-1", "auth_kind": "access_key",
                  "tag_project": "armature-platform", "install_id": "i1", "allow_host_allocation": False,
                  "max_concurrent": 1, "max_daily_launches": 1, "boot_timeout_s": 420, "max_lifetime_s": 5400,
                  "state": "ok", "created_at": utcnow(), "updated_at": utcnow()}
    try:
        with engine.begin() as conn:
            assert conn.execute(select(projects.c.environment_type_id)).scalar_one() is None
            conn.execute(insert(aws_connections).values(**connection))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(aws_connections).values(**{**connection, "id": new_id(), "name": "ops",
                                                               "tag_project": "armature"}))
            conn.execute(update(jobs).values(status="provisioning"))
            conn.execute(insert(environments).values(id=new_id(), connection_id=connection["id"], purpose="job",
                                                     job_id=job, state="booting", launch_attempts=1,
                                                     requested_at=utcnow()))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0013")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            status = conn.execute(text("SELECT status FROM jobs")).scalar_one()
        names = set(inspect(engine).get_table_names())
        assert "environment_type_id" not in {c["name"] for c in inspect(engine).get_columns("projects")}
    finally:
        engine.dispose()
    assert status == "failed" and not names & {"aws_connections", "environment_types", "environment_hosts",
                                                "environments"}


def test_0016_keeps_live_webhooks_and_downgrade_disables_linked_ones(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import connections
    migrate(db_url, "0015")
    team, hook, delivery = new_id(), new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-30 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            conn.execute(text("INSERT INTO webhooks (id, team_id, name, source, path_token, secret_name, enabled, "
                              "approval, created_at, updated_at) VALUES (:h, :t, 'gh', 'github', 'tok', 'S', :on, "
                              "'approved', :at, :at)"), {"h": hook, "t": team, "on": True, **at})
            conn.execute(text("INSERT INTO webhook_deliveries (id, webhook_id, received_at, status) "
                              "VALUES (:d, :h, :at, 'accepted')"), {"d": delivery, "h": hook, **at})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    connection = {"id": new_id(), "team_id": team, "scope": team, "kind": "slack", "name": "main",
                  "state": "unverified", "created_at": utcnow(), "updated_at": utcnow()}
    try:
        with engine.begin() as conn:
            row = conn.execute(select(webhooks).where(webhooks.c.id == hook)).one()
            kept = conn.execute(select(webhook_deliveries.c.webhook_id)).scalars().all()
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(update(webhooks).values(secret_name=None))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(connections).values(**{**connection, "kind": "smtp"}))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(connections).values(**{**connection, "scope": new_id()}))
            conn.execute(insert(connections).values(**connection))
            conn.execute(text("INSERT INTO webhooks (id, team_id, name, source, path_token, connection_id, enabled, "
                              "approval, created_at, updated_at) VALUES (:h, :t, 'slack', 'slack', 'tok2', :c, :on, "
                              "'approved', :at, :at)"),
                         {"h": new_id(), "t": team, "c": connection["id"], "on": True, "at": "2026-10-31 12:00:00"})
    finally:
        engine.dispose()
    assert (row.secret_name, row.connection_id, row.enabled) == ("S", None, True) and kept == [hook]
    command.downgrade(alembic_config(db_url), "0015")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = {r.name: r for r in conn.execute(text("SELECT name, secret_name, enabled FROM webhooks")).all()}
        names = set(inspect(engine).get_table_names())
        columns = {c["name"] for c in inspect(engine).get_columns("webhooks")}
    finally:
        engine.dispose()
    assert not names & {"connections", "github_installations"} and "connection_id" not in columns
    assert rows["gh"].secret_name == "S" and bool(rows["gh"].enabled)
    assert rows["slack"].secret_name == "WEBHOOK_SECRET" and not rows["slack"].enabled


def test_0017_allows_skill_documents_and_downgrade_drops_them(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import versions
    migrate(db_url)
    engine = make_engine(db_url)
    skill, role = new_id(), new_id()
    try:
        with engine.begin() as conn:
            for doc_id, kind in ((skill, "skill"), (role, "role")):
                conn.execute(insert(documents).values(id=doc_id, kind=kind, key=f"k-{kind}", current_version=1,
                                                      created_at=utcnow(), updated_at=utcnow()))
                conn.execute(insert(versions).values(id=new_id(), object_kind="document", object_id=doc_id,
                                                     version=1, body="b", created_at=utcnow()))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(documents).values(id=new_id(), kind="hook", key="h", current_version=0,
                                                      created_at=utcnow(), updated_at=utcnow()))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0016")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            kinds = conn.execute(text("SELECT kind FROM documents")).scalars().all()
            owners = set(conn.execute(text("SELECT object_id FROM versions")).scalars())
    finally:
        engine.dispose()
    assert kinds == ["role"] and owners == {role}


def test_0020_adds_risk_and_outcomes_seeds_the_rubric_once_and_downgrade_keeps_it(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import findings, versions
    from armature.server.defs import validate_document
    migrate(db_url)
    engine = make_engine(db_url)
    team_id, run_id = new_id(), new_id()
    try:
        with engine.begin() as conn:
            conn.execute(insert(teams).values(id=team_id, name="risk", created_at=utcnow()))
            conn.execute(insert(runs).values(id=run_id, team_id=team_id, status="succeeded", started_at=utcnow(),
                                             risk="high", outcome="recommended"))
            for column, value in (("risk", "severe"), ("outcome", "done")):
                with pytest.raises(CHECK_FAILED), conn.begin_nested():
                    conn.execute(update(runs).where(runs.c.id == run_id).values(**{column: value}))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(findings).values(id=new_id(), run_id=run_id, finding_key="k", decision="maybe"))
            rubric = conn.execute(select(documents.c.id, documents.c.current_version, versions.c.body)
                                  .join(versions, versions.c.object_id == documents.c.id)
                                  .where(documents.c.kind == "skill", documents.c.key == "risk-rubric",
                                         documents.c.scope == "*")).all()
    finally:
        engine.dispose()
    assert len(rubric) == 1 and rubric[0].current_version == 1
    assert validate_document("skill", rubric[0].body, actor=None, previous_body=None, key="risk-rubric").ok
    command.downgrade(alembic_config(db_url), "0017")
    engine = make_engine(db_url)
    try:
        assert "review_marks" not in inspect(engine).get_table_names()
        assert "risk" not in {c["name"] for c in inspect(engine).get_columns("runs")}
        assert "decision" not in {c["name"] for c in inspect(engine).get_columns("findings")}
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            seeded = conn.execute(select(documents.c.id).where(documents.c.key == "risk-rubric")).all()
    finally:
        engine.dispose()
    assert len(seeded) == 1


def test_0018_adds_tool_calls_and_downgrade_drops_them(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import tool_calls
    migrate(db_url)
    engine = make_engine(db_url)
    team_id, run_id = new_id(), new_id()
    try:
        with engine.begin() as conn:
            conn.execute(insert(teams).values(id=team_id, name="mobile", created_at=utcnow()))
            conn.execute(insert(runs).values(id=run_id, team_id=team_id, status="running", started_at=utcnow()))
            conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=team_id, step_id="post",
                                                   tool="slack.post", mode="dry", outcome="ok", created_at=utcnow()))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=team_id, step_id="post",
                                                       tool="slack.post", mode="stub", outcome="ok",
                                                       created_at=utcnow()))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0021")
    engine = make_engine(db_url)
    try:
        assert "tool_calls" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_0019_adds_gates_and_downgrade_fails_runs_waiting_at_one(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import gates, jobs, users
    migrate(db_url)
    engine = make_engine(db_url)
    team, run, job, waiting = new_id(), new_id(), new_id(), new_id()
    try:
        with engine.begin() as conn:
            conn.execute(insert(teams).values(id=team, name="own", created_at=utcnow()))
            conn.execute(insert(runs).values([
                {"id": run, "team_id": team, "status": "waiting", "started_at": utcnow()},
                {"id": waiting, "team_id": team, "status": "succeeded", "started_at": utcnow()}]))
            gate = {"id": new_id(), "run_id": run, "team_id": team, "step_id": "approve", "step_key": "approve",
                    "state": "open", "min_role": "project_lead", "nonce": "n", "created_at": utcnow(),
                    "expires_at": utcnow()}
            conn.execute(insert(gates).values(**gate))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(insert(gates).values(**{**gate, "id": new_id()}))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(gates).values(**{**gate, "id": new_id(), "step_key": "b", "state": "maybe"}))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(gates).values(**{**gate, "id": new_id(), "step_key": "c", "min_role": "boss"}))
            for name in ("a", "b"):
                conn.execute(insert(users).values(id=new_id(), username=name, last_totp_step=0, is_admin=False,
                                                  disabled=False, created_at=utcnow(), slack_workspace_id="T1",
                                                  slack_user_id=f"U{name}"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(update(users).where(users.c.username == "b").values(slack_user_id="Ua"))
            conn.execute(text("INSERT INTO workflows (id, team_id, scope, name, kind, current_version, created_at, "
                              "updated_at) VALUES (:w, :t, :t, 'ship', 'other', 1, :at, :at)"),
                         {"w": job, "t": team, "at": utcnow()})
            conn.execute(insert(jobs).values(id=job, team_id=team, workflow_id=job, run_id=run, status="suspended",
                                             attempts=0, created_at=utcnow(), thread="slack:C1:1.2"))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0018")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            statuses = dict(conn.execute(text("SELECT id, status FROM runs")).all())
            job_status = conn.execute(text("SELECT status FROM jobs")).scalar_one()
        names = set(inspect(engine).get_table_names())
        job_columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    finally:
        engine.dispose()
    assert statuses == {run: "failed", waiting: "succeeded"} and job_status == "failed"
    assert "gates" not in names and not job_columns & {"thread", "resume_of_gate"}


def test_0024_adds_run_location_and_runner_platform_and_downgrade_drops_them(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    migrate(db_url, "0019")
    team_id, run_id = new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            stamp = {"at": "2026-10-28 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'live', :at)"),
                         {"t": team_id, **stamp})
            conn.execute(text("INSERT INTO runs (id, team_id, status, started_at) VALUES (:r, :t, 'succeeded', :at)"),
                         {"r": run_id, "t": team_id, **stamp})
    finally:
        engine.dispose()
    migrate(db_url)
    mac = {"kind": "aws-mac", "label": "AWS macOS"}
    engine = make_engine(db_url)
    try:
        assert "platform" in {c["name"] for c in inspect(engine).get_columns("runners")}
        assert ["run_id"] in [ix["column_names"] for ix in inspect(engine).get_indexes("jobs")]
        with engine.begin() as conn:
            assert conn.execute(select(runs.c.location).where(runs.c.id == run_id)).scalar() is None
            conn.execute(update(runs).where(runs.c.id == run_id).values(location=mac))
            assert conn.execute(select(runs.c.location)).scalar_one() == mac
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0019")
    engine = make_engine(db_url)
    try:
        assert "location" not in {c["name"] for c in inspect(engine).get_columns("runs")}
        assert "platform" not in {c["name"] for c in inspect(engine).get_columns("runners")}
        assert ["run_id"] not in [ix["column_names"] for ix in inspect(engine).get_indexes("jobs")]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT id FROM runs")).scalars().all() == [run_id]
    finally:
        engine.dispose()


def test_0023_adds_conversations_and_downgrade_fails_their_queued_turns(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import conversation_messages, conversations, jobs
    migrate(db_url)
    engine = make_engine(db_url)
    team, workflow, job, conv = new_id(), new_id(), new_id(), new_id()
    try:
        with engine.begin() as conn:
            conn.execute(insert(teams).values(id=team, name="own", created_at=utcnow()))
            conn.execute(text("INSERT INTO workflows (id, team_id, scope, name, kind, current_version, created_at, "
                              "updated_at) VALUES (:w, :t, :t, 'intake', 'other', 1, :at, :at)"),
                         {"w": workflow, "t": team, "at": utcnow()})
            conn.execute(insert(jobs).values(id=job, team_id=team, workflow_id=workflow, status="queued", attempts=0,
                                             created_at=utcnow()))
            row = {"id": conv, "team_id": team, "workflow_id": workflow, "kind": "slack", "thread": "slack:C1:1.2",
                   "state": "open", "awaiting": "agent", "turns": 1, "active_job_id": job, "created_at": utcnow(),
                   "updated_at": utcnow()}
            conn.execute(insert(conversations).values(**row))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(insert(conversations).values(**{**row, "id": new_id(), "active_job_id": None}))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(conversations).values(**{**row, "id": new_id(), "thread": "web:x", "kind": "fax",
                                                             "active_job_id": None}))
            message = {"conversation_id": conv, "role": "user", "text": "hi", "handled": False, "created_at": utcnow()}
            conn.execute(insert(conversation_messages).values(id=new_id(), seq=1, ext_ts="1.2", **message))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(insert(conversation_messages).values(id=new_id(), seq=2, ext_ts="1.2", **message))
            conn.execute(insert(conversation_messages).values(id=new_id(), seq=2, **message))
            conn.execute(insert(conversation_messages).values(id=new_id(), seq=3, **message))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0019")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            status = conn.execute(text("SELECT status FROM jobs")).scalar_one()
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert status == "failed" and not names & {"conversations", "conversation_messages", "slack_links"}


def test_0025_adds_job_dry_run_defaulting_off_and_downgrade_drops_it(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    migrate(db_url)
    engine = make_engine(db_url)
    team, job = new_id(), new_id()
    try:
        with engine.begin() as conn:
            conn.execute(insert(teams).values(id=team, name="own", created_at=utcnow()))
            conn.execute(text("INSERT INTO workflows (id, team_id, scope, name, kind, current_version, created_at, "
                              "updated_at) VALUES (:w, :t, :t, 'ship', 'other', 1, :at, :at)"),
                         {"w": job, "t": team, "at": utcnow()})
            conn.execute(text("INSERT INTO jobs (id, team_id, workflow_id, status, attempts, kind, created_at) "
                              "VALUES (:j, :t, :j, 'queued', 0, 'workflow', :at)"),
                         {"j": job, "t": team, "at": utcnow()})
            assert not conn.execute(text("SELECT dry_run FROM jobs")).scalar_one()
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0023")
    engine = make_engine(db_url)
    try:
        assert "dry_run" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    finally:
        engine.dispose()



def test_0026_adds_job_rerun_columns_and_downgrade_drops_them(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        assert {"rerun_of", "requested_by"} <= {c["name"] for c in inspect(engine).get_columns("jobs")}
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0022")
    engine = make_engine(db_url)
    try:
        assert not {"rerun_of", "requested_by"} & {c["name"] for c in inspect(engine).get_columns("jobs")}
    finally:
        engine.dispose()


def test_0022_adds_poll_state_and_downgrade_drops_poll_triggers(db_url):
    from alembic import command

    from armature.server.db import alembic_config
    from armature.server.db.schema import jobs, trigger_seen, trigger_state, triggers
    migrate(db_url)
    engine = make_engine(db_url)
    team, workflow, poll, hook_rule, job, newer = (new_id() for _ in range(6))
    try:
        with engine.begin() as conn:
            conn.execute(insert(teams).values(id=team, name="own", created_at=utcnow()))
            conn.execute(text("INSERT INTO workflows (id, team_id, scope, name, kind, current_version, created_at, "
                              "updated_at) VALUES (:w, :t, :t, 'work', 'other', 1, :at, :at)"),
                         {"w": workflow, "t": team, "at": utcnow()})
            conn.execute(insert(triggers).values([
                {"id": poll, "team_id": team, "workflow_id": workflow, "kind": "jira_poll", "config": {},
                 "enabled": True, "created_at": utcnow()},
                {"id": hook_rule, "team_id": team, "workflow_id": workflow, "kind": "webhook", "config": {},
                 "enabled": True, "created_at": utcnow()}]))
            conn.execute(insert(trigger_state).values(trigger_id=poll, failures=0, next_run_at=utcnow()))
            seen = {"trigger_id": poll, "item_key": "ENG-1", "outcome": "seeded", "first_seen_at": utcnow(),
                    "last_seen_at": utcnow()}
            conn.execute(insert(trigger_seen).values(**seen))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(insert(trigger_seen).values(**seen))
            with pytest.raises(CHECK_FAILED), conn.begin_nested():
                conn.execute(insert(trigger_seen).values(**{**seen, "item_key": "ENG-2", "outcome": "maybe"}))
            conn.execute(insert(jobs).values(id=job, team_id=team, workflow_id=workflow, status="superseded",
                                             attempts=0, created_at=utcnow(), trigger_id=poll, concurrency_key="a#1",
                                             superseded_by=newer))
            conn.execute(insert(jobs).values(id=newer, team_id=team, workflow_id=workflow, status="queued",
                                             attempts=0, created_at=utcnow(), trigger_id=hook_rule,
                                             concurrency_key="a#1", not_before=utcnow()))
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0025")
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            kinds = conn.execute(text("SELECT kind FROM triggers")).scalars().all()
            found = dict(conn.execute(text("SELECT id, status FROM jobs")).all())
            linked = conn.execute(text("SELECT trigger_id FROM jobs WHERE id = :j"), {"j": job}).scalar()
        names = set(inspect(engine).get_table_names())
        job_columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    finally:
        engine.dispose()
    assert kinds == ["webhook"] and linked is None
    assert found == {job: "canceled", newer: "queued"}
    assert not names & {"trigger_state", "trigger_seen", "trigger_polls"}
    assert not job_columns & {"concurrency_key", "not_before", "superseded_by"}
