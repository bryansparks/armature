import pytest
from sqlalchemy import insert, select, text, update

from armature.server import secrets
from armature.server.db import new_id, utcnow
from armature.server.db.schema import audit, teams
from armature.server.db.schema import secrets as secrets_table


@pytest.fixture
def team_ids(sqlite_engine):
    ids = [new_id(), new_id()]
    with sqlite_engine.begin() as conn:
        for index, team_id in enumerate(ids):
            conn.execute(insert(teams).values(id=team_id, name=f"team{index}", created_at=utcnow()))
    return ids


def test_list_never_returns_plaintext(sqlite_engine, keyring, team_ids):
    with sqlite_engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=team_ids[0], name="GH_TOKEN", value="plain-value-123",
                           actor_id="u1")
        listed = secrets.list_secrets(conn, team_ids[0])
    assert [s["name"] for s in listed] == ["GH_TOKEN"]
    assert set(listed[0]) == {"name", "set", "updated_at", "updated_by"}
    assert "plain-value-123" not in repr(listed)


def test_set_updates_in_place_and_audits(sqlite_engine, keyring, team_ids):
    with sqlite_engine.begin() as conn:
        first = secrets.set_secret(conn, keyring, team_id=None, name="K", value="s3cr3t-a", actor_id="u1")
        second = secrets.set_secret(conn, keyring, team_id=None, name="K", value="s3cr3t-b", actor_id="u2")
        assert first == second
        assert secrets.resolve_for_run(conn, keyring, team_ids[0], ["K"]) == {"K": "s3cr3t-b"}
        assert secrets.delete_secret(conn, team_id=None, name="K", actor_id="u3")
        assert not secrets.delete_secret(conn, team_id=None, name="K", actor_id="u3")
        actions = sorted(conn.execute(select(audit.c.action)).scalars())
        details = repr(conn.execute(select(audit.c.detail)).all())
    assert actions == ["secret.delete", "secret.set", "secret.set"]
    assert "s3cr3t" not in details


def test_team_secret_overrides_instance_and_isolated(sqlite_engine, keyring, team_ids):
    with sqlite_engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=None, name="K", value="instance", actor_id=None)
        secrets.set_secret(conn, keyring, team_id=team_ids[0], name="K", value="team0", actor_id=None)
        assert secrets.resolve_for_run(conn, keyring, team_ids[0], ["K"]) == {"K": "team0"}
        assert secrets.resolve_for_run(conn, keyring, team_ids[1], ["K"]) == {"K": "instance"}
        assert secrets.list_secrets(conn, team_ids[1]) == []


def test_missing_secret_fails(sqlite_engine, keyring, team_ids):
    with sqlite_engine.begin() as conn, pytest.raises(secrets.MissingSecrets):
        secrets.resolve_for_run(conn, keyring, team_ids[0], ["ABSENT"])


def test_ciphertext_copied_to_another_team_fails(sqlite_engine, keyring, team_ids):
    from armature.server.crypto import SealError
    with sqlite_engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=team_ids[0], name="K", value="team0", actor_id=None)
        secrets.set_secret(conn, keyring, team_id=team_ids[1], name="K", value="team1", actor_id=None)
        stolen = conn.execute(select(secrets_table.c.ciphertext)
                              .where(secrets_table.c.team_id == team_ids[0])).scalar_one()
        conn.execute(update(secrets_table).where(secrets_table.c.team_id == team_ids[1]).values(ciphertext=stolen))
        with pytest.raises(SealError):
            secrets.resolve_for_run(conn, keyring, team_ids[1], ["K"])


@pytest.mark.parametrize("name", ["", "1BAD", "has-dash", "x" * 129])
def test_bad_names_refused(sqlite_engine, keyring, name):
    with sqlite_engine.begin() as conn, pytest.raises(ValueError):
        secrets.set_secret(conn, keyring, team_id=None, name=name, value="v", actor_id=None)


def test_tampering_only_scope_fails_to_decrypt(sqlite_engine, keyring, team_ids):
    from armature.server.crypto import SealError
    a, b = team_ids
    with sqlite_engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=a, name="K", value="team-A-secret", actor_id=None)
        conn.execute(text("PRAGMA ignore_check_constraints=ON"))
        conn.execute(update(secrets_table).where(secrets_table.c.team_id == a).values(scope=b))
        with pytest.raises(SealError):
            secrets.resolve_for_run(conn, keyring, b, ["K"])


def test_moving_a_team_secret_to_instance_scope_fails_to_decrypt(sqlite_engine, keyring, team_ids):
    from armature.server.crypto import SealError
    with sqlite_engine.begin() as conn:
        secrets.set_secret(conn, keyring, team_id=team_ids[0], name="K", value="team-A-secret", actor_id=None)
        conn.execute(update(secrets_table).values(team_id=None, scope="*"))
        with pytest.raises(SealError):
            secrets.resolve_for_run(conn, keyring, team_ids[1], ["K"])
