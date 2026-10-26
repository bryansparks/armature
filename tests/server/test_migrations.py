from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError

from armature.server import secrets
from armature.server.db import make_engine, migrate, new_id, utcnow
from armature.server.db.schema import documents, metadata, run_events, runs, teams, workflows
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
