import os

import pyotp
from sqlalchemy import select
from typer.testing import CliRunner

from armature.cli import app
from armature.server.config import load_config
from armature.server.crypto import keyring_from_source
from armature.server.db import make_engine
from armature.server.db.schema import users
from armature.server.passwords import verify_password
from armature.server.users import totp_aad

PASSWORD = "correct horse battery"
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def _init(tmp_path, monkeypatch, *extra, codes=None, totp=True):
    monkeypatch.setenv("ARMATURE_SERVER_REQUIRE_TOTP", "true" if totp else "false")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr("armature.server.users.pyotp.random_base32", lambda: SECRET)
    config = tmp_path / "server.yaml"
    args = ["server", "init", "--config", str(config), "--database-url", f"sqlite:///{tmp_path / 'db.sqlite'}",
            "--key-file", str(tmp_path / "master.key"), "--username", "admin", *extra]
    typed = codes if codes is not None else [pyotp.TOTP(SECRET).now()]
    return config, CliRunner().invoke(app, args, input="".join(f"{code}\n" for code in typed))


def test_init_creates_config_key_schema_and_admin(tmp_path, monkeypatch):
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert os.stat(tmp_path / "master.key").st_mode & 0o777 == 0o600
    assert os.stat(config).st_mode & 0o777 == 0o600
    assert PASSWORD not in result.output
    uri = next(line for line in result.output.splitlines() if line.startswith("otpauth://"))

    cfg = load_config(config)
    engine = make_engine(cfg.database_url)
    with engine.connect() as conn:
        row = conn.execute(select(users)).one()
    engine.dispose()
    assert row.username == "admin" and row.is_admin
    assert verify_password(PASSWORD, row.password_hash)
    secret = keyring_from_source(cfg.master_key).open_(row.totp_secret_enc, totp_aad(row.id), label="totp")
    assert pyotp.parse_uri(uri).secret == secret


def test_create_admin_confirms_the_authenticator(tmp_path, monkeypatch):
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Authenticator confirmed." in result.output
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        row = conn.execute(select(users)).one()
    engine.dispose()
    assert row.totp_confirmed_at is not None and row.last_totp_step > 0


def test_create_admin_without_totp_asks_for_no_code(tmp_path, monkeypatch):
    config, result = _init(tmp_path, monkeypatch, codes=[], totp=False)
    assert result.exit_code == 0, result.output
    assert "otpauth://" not in result.output and "Code from the authenticator" not in result.output
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        row = conn.execute(select(users)).one()
    engine.dispose()
    assert row.is_admin and verify_password(PASSWORD, row.password_hash) and row.totp_secret_enc is None
    _, again = _init(tmp_path, monkeypatch, codes=[], totp=False)
    assert "already exists" in again.output


def test_create_admin_removed_after_three_wrong_codes(tmp_path, monkeypatch):
    wrong = "000000" if pyotp.TOTP(SECRET).now() != "000000" else "111111"
    config, result = _init(tmp_path, monkeypatch, codes=[wrong] * 3)
    assert result.exit_code == 1
    assert "admin was removed" in result.output
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        assert conn.execute(select(users)).all() == []
    engine.dispose()


def test_init_again_keeps_the_admin(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "already exists" in result.output
    assert "otpauth://" not in result.output


def test_init_refuses_a_world_readable_key(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch, "--no-admin")
    os.chmod(tmp_path / "master.key", 0o644)
    _, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 1
    assert "chmod 600" in result.output


def test_create_admin_has_no_password_option():
    result = CliRunner().invoke(app, ["server", "create-admin", "--password", "x"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_init_refuses_a_new_key_over_sealed_values(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    (tmp_path / "master.key").unlink()
    _, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 1
    assert "Restore the key file" in result.output
    assert not (tmp_path / "master.key").exists()


def test_init_makes_a_new_key_when_nothing_is_sealed(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch, "--no-admin")
    (tmp_path / "master.key").unlink()
    _, result = _init(tmp_path, monkeypatch, "--no-admin")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "master.key").exists()


def test_init_takes_database_url_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    url = f"sqlite:///{tmp_path / 'env.sqlite'}"
    monkeypatch.setenv("ARMATURE_SERVER_DATABASE_URL", url)
    config = tmp_path / "server.yaml"
    result = CliRunner().invoke(app, ["server", "init", "--config", str(config), "--key-file",
                                      str(tmp_path / "master.key"), "--no-admin"])
    assert result.exit_code == 0, result.output
    monkeypatch.delenv("ARMATURE_SERVER_DATABASE_URL")
    assert load_config(config).database_url == url
    assert (tmp_path / "env.sqlite").exists()


def test_init_warns_about_a_password_in_the_url(tmp_path, monkeypatch):
    config = tmp_path / "server.yaml"
    result = CliRunner().invoke(app, ["server", "init", "--config", str(config), "--database-url",
                                      "postgresql+psycopg://u:hunter2@127.0.0.1:1/x", "--key-file",
                                      str(tmp_path / "master.key"), "--no-admin"])
    assert "holds a password" in result.output


def test_run_refuses_without_an_admin(tmp_path, monkeypatch):
    config, _ = _init(tmp_path, monkeypatch, "--no-admin")
    result = CliRunner().invoke(app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 1
    assert "No admin exists" in result.output


def test_run_refuses_without_the_master_key(tmp_path, monkeypatch):
    config, _ = _init(tmp_path, monkeypatch)
    (tmp_path / "master.key").unlink()
    result = CliRunner().invoke(app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 1
    assert "Master key unavailable" in result.output


def test_run_starts_uvicorn_with_the_app(tmp_path, monkeypatch):
    config, _ = _init(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app_, **kwargs: seen.update(app=app_, **kwargs))
    result = CliRunner().invoke(app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert seen["workers"] == 1 and seen["proxy_headers"] is False and seen["port"] == 8098
    assert "ARMATURE_MASTER_KEY" not in os.environ


def test_create_admin_interrupted_leaves_no_admin(tmp_path, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("typer.prompt", interrupted)
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code != 0
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        assert conn.execute(select(users)).all() == []
    engine.dispose()


def test_run_ignores_an_unconfirmed_admin(tmp_path, monkeypatch):
    from sqlalchemy import update
    config, _ = _init(tmp_path, monkeypatch)
    engine = make_engine(load_config(config).database_url)
    with engine.begin() as conn:
        conn.execute(update(users).values(totp_confirmed_at=None))
    engine.dispose()
    result = CliRunner().invoke(app, ["server", "run", "--config", str(config)])
    assert result.exit_code == 1 and "No admin exists" in result.output


def test_runs_relink_by_id_or_unique_name(tmp_path, monkeypatch):
    from sqlalchemy import insert

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import projects, runs, teams, workflows
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    own, other, project = new_id(), new_id(), new_id()
    check, own_dup, project_dup = new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=own, name="own", slug="own", created_at=utcnow()))
        conn.execute(insert(teams).values(id=other, name="other", slug="other", created_at=utcnow()))
        conn.execute(insert(projects).values(id=project, team_id=own, name="app", created_at=utcnow()))
        for workflow_id, team, project_id, name in ((check, own, None, "pr-check"), (own_dup, own, None, "dup"),
                                                    (project_dup, own, project, "dup")):
            conn.execute(insert(workflows).values(id=workflow_id, team_id=team, project_id=project_id, name=name,
                                                  current_version=1, created_at=utcnow(), updated_at=utcnow()))
        for team, name in ((own, "code-review"), (own, "code-review"), (other, "code-review"), (own, "old-dup")):
            conn.execute(insert(runs).values(id=new_id(), team_id=team, workflow_name=name, status="succeeded",
                                             started_at=utcnow()))

    def relink(old, wanted, *extra):
        return CliRunner().invoke(app, ["server", "runs", "relink", "--config", str(config), "--from", old,
                                        "--to", wanted, *extra])

    def linked():
        with engine.connect() as conn:
            return sorted((r.workflow_name, r.team_id == own, r.workflow_id or "") for r in conn.execute(select(runs)))

    before = linked()
    dry = relink("code-review", "pr-check", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "Would link 2 runs of code-review to pr-check (team own)." in dry.output
    assert linked() == before
    done = relink("code-review", "pr-check")
    assert done.exit_code == 0, done.output
    assert "Linked 2 runs" in done.output
    assert linked() == [("code-review", False, ""), ("code-review", True, check), ("code-review", True, check),
                        ("old-dup", True, "")]
    ambiguous = relink("old-dup", "dup")
    assert ambiguous.exit_code == 1
    assert own_dup in ambiguous.output and project_dup in ambiguous.output
    assert "team own" in ambiguous.output and "project app" in ambiguous.output
    by_id = relink("old-dup", own_dup)
    assert by_id.exit_code == 0 and "Linked 1 run of old-dup to dup (team own)." in by_id.output
    missing = relink("old-dup", "nothing")
    assert missing.exit_code == 1 and "No workflow has the id or name nothing" in missing.output
    engine.dispose()


def test_backfill_locations_marks_unplaceable_runs_unknown_once(tmp_path, monkeypatch):
    from sqlalchemy import insert

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import runs, teams
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team = new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", slug="own", created_at=utcnow()))
        conn.execute(insert(runs).values(id=new_id(), team_id=team, status="succeeded", started_at=utcnow()))
    engine.dispose()
    first = CliRunner().invoke(app, ["server", "backfill-locations", "--config", str(config)])
    assert first.exit_code == 0 and "Located 1 run (1 unknown)." in first.output, first.output
    again = CliRunner().invoke(app, ["server", "backfill-locations", "--config", str(config)])
    assert again.exit_code == 0 and "Located 0 runs." in again.output, again.output


def test_backfill_rounds_reports_the_subjects_it_derived_and_the_findings_it_rekeyed(tmp_path, monkeypatch):
    import hashlib

    from sqlalchemy import insert, select

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import findings, projects, runs, teams
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team, project, run_id = new_id(), new_id(), new_id()
    legacy = hashlib.sha256(b"a.py|1|It leaks.").hexdigest()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", slug="own", created_at=utcnow()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", repo="acme/app",
                                             created_at=utcnow()))
        conn.execute(insert(runs).values(id=run_id, team_id=team, project_id=project, status="succeeded",
                                         started_at=utcnow(), subject="acme/app#1"))
        conn.execute(insert(findings).values(id=new_id(), run_id=run_id, project_id=project, finding_key=legacy,
                                             path="a.py", line=1, body="It leaks."))
    first = CliRunner().invoke(app, ["server", "backfill-rounds", "--config", str(config)])
    assert first.exit_code == 0, first.output
    assert "Derived the review rounds of 1 pull request and re-keyed 1 finding." in first.output
    with engine.connect() as conn:
        assert len(conn.execute(select(findings.c.finding_key)).scalar_one()) == 12
    engine.dispose()
    again = CliRunner().invoke(app, ["server", "backfill-rounds", "--config", str(config)])
    assert again.exit_code == 0 and "re-keyed 0 findings." in again.output, again.output


def test_backfill_candidates_reports_what_it_stored_and_skipped(tmp_path, monkeypatch):
    from sqlalchemy import insert

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import run_steps, runs, teams, tool_calls
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team, run_id = new_id(), new_id()
    found = {"findings": [{"path": "a.py", "line": 1, "claim": "it leaks", "body": "b"}]}
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", slug="own", created_at=utcnow()))
        conn.execute(insert(runs).values(id=run_id, team_id=team, status="succeeded", started_at=utcnow()))
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=team, step_id="decisions",
                                               tool="review.decisions", mode="live", outcome="ok",
                                               created_at=utcnow()))
        conn.execute(insert(run_steps).values([
            {"run_id": run_id, "step_key": "correctness", "step_id": "correctness", "status": "succeeded",
             "output": found},
            {"run_id": run_id, "step_key": "security", "step_id": "security", "status": "succeeded",
             "output": {**found, "_truncated": True}}]))
    engine.dispose()
    first = CliRunner().invoke(app, ["server", "backfill-candidates", "--config", str(config)])
    assert first.exit_code == 0, first.output
    assert ("Stored 1 candidates from 1 specialist steps, and matched 1 runs: 0 by key, 0 by path and claim, "
            "1 unmatched. Skipped 1 truncated step outputs.") in first.output
    again = CliRunner().invoke(app, ["server", "backfill-candidates", "--config", str(config)])
    assert again.exit_code == 0 and "Stored 0 candidates" in again.output, again.output


def test_label_findings_names_the_alias_it_chose_and_what_it_spent(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from sqlalchemy import insert

    from armature.server import llm
    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import (
        finding_candidates,
        labels,
        providers,
        runs,
        teams,
    )
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team, run_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="Mobile", slug="mobile", created_at=utcnow()))
        conn.execute(insert(providers).values(id=new_id(), team_id=team, kind="anthropic", config={},
                                              aliases={"lead": "claude-opus-5-5", "quick": "claude-haiku-5-5"},
                                              created_at=utcnow(), updated_at=utcnow()))
        conn.execute(insert(runs).values(id=run_id, team_id=team, status="succeeded", started_at=utcnow()))
        conn.execute(insert(finding_candidates).values(id=new_id(), team_id=team, run_id=run_id,
                                                       step_key="correctness", specialist="correctness", ordinal=0,
                                                       claim="it leaks", created_at=utcnow()))
    engine.dispose()
    answer = {"labels": [{"id": "0", "category": "lifecycle-memory", "confidence": 0.8}]}
    monkeypatch.setattr(llm, "litellm_completion", lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))],
        usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5), _hidden_params={"response_cost": 0.002}))
    missing = CliRunner().invoke(app, ["server", "label-findings", "--team", "web", "--config", str(config)])
    assert missing.exit_code == 1 and "No team has the slug or name web." in missing.output
    engine = make_engine(load_config(config).database_url)
    with engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": new_id(), "name": "Alpha", "slug": "beta", "created_at": utcnow()},
                                           {"id": new_id(), "name": "beta", "slug": "gamma", "created_at": utcnow()}]))
    engine.dispose()
    clash = CliRunner().invoke(app, ["server", "label-findings", "--team", "beta", "--config", str(config)])
    assert clash.exit_code == 1 and "is the slug of one team and the name of another" in clash.output, clash.output
    done = CliRunner().invoke(app, ["server", "label-findings", "--team", "Mobile", "--config", str(config)])
    assert done.exit_code == 0, done.output
    assert "Labeling with quick, because its name or model (claude-haiku-5-5) suggests a small model." in done.output
    assert "Labeled 1 with taxonomy v1 in 1 calls, 50 input and 5 output tokens, 0.0020 USD." in done.output
    engine = make_engine(load_config(config).database_url)
    with engine.connect() as conn:
        row = conn.execute(select(labels)).one()
    engine.dispose()
    assert (row.label, row.labeler) == ("lifecycle-memory", "quick:anthropic/claude-haiku-5-5")


def test_sync_github_claims_and_runs_each_matching_state(tmp_path, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import insert

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import gh_repos, gh_sync_state, teams
    from armature.server.insights import sync
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team, repo = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="Mobile", slug="mobile", created_at=utcnow()))
        conn.execute(insert(gh_repos).values(id=repo, team_id=team, gh_repo_id=4242, full_name="acme/app",
                                             enabled=True, created_at=utcnow(), updated_at=utcnow()))
        conn.execute(insert(gh_sync_state), [
            {"id": new_id(), "team_id": team, "repo_row_id": repo, "kind": kind, "updated_at": utcnow(),
             "next_run_at": None if kind == "backfill" else utcnow() - timedelta(minutes=1)} for kind in sync.KINDS])
    engine.dispose()
    ran = []
    monkeypatch.setattr(sync, "run", lambda engine, keyring, state_id, lease, **kw: ran.append(state_id) or "ok")
    wrong = CliRunner().invoke(app, ["server", "sync-github", "--team", "mobile", "--kind", "nightly",
                                     "--config", str(config)])
    assert wrong.exit_code == 1 and "--kind must be one of" in wrong.output
    none = CliRunner().invoke(app, ["server", "sync-github", "--team", "mobile", "--repo", "acme/lib",
                                    "--config", str(config)])
    assert none.exit_code == 1 and "No enabled tracked repository" in none.output
    poll = CliRunner().invoke(app, ["server", "sync-github", "--team", "mobile", "--kind", "poll",
                                    "--config", str(config)])
    assert (poll.exit_code, poll.output.splitlines(), len(ran)) == (0, ["acme/app poll: ok"], 1), poll.output
    engine = make_engine(load_config(config).database_url)
    with engine.begin() as conn:
        conn.execute(gh_sync_state.update().values(next_run_at=utcnow() - timedelta(minutes=1))
                     .where(gh_sync_state.c.kind != "backfill"))
    engine.dispose()
    ran.clear()
    done = CliRunner().invoke(app, ["server", "sync-github", "--team", "Mobile", "--repo", "Acme/App",
                                    "--config", str(config)])
    assert done.exit_code == 0, done.output
    assert done.output.splitlines() == ["acme/app backfill: done", "acme/app conflicts: ok", "acme/app gap_fill: ok",
                                        "acme/app poll: ok"]
    assert len(ran) == 3


def test_runs_delete_needs_an_admin_and_can_dry_run(tmp_path, monkeypatch):
    from sqlalchemy import func, insert

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import audit, jobs, run_events, runs, teams, workflows
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team, ended, running, workflow = new_id(), new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", slug="own", created_at=utcnow()))
        conn.execute(insert(workflows).values(id=workflow, team_id=team, name="test-run", current_version=1,
                                              created_at=utcnow(), updated_at=utcnow()))
        conn.execute(insert(users).values(id=new_id(), username="viewer", is_admin=False, disabled=False,
                                          created_at=utcnow()))
        for run_id, status in ((ended, "succeeded"), (running, "running")):
            conn.execute(insert(runs).values(id=run_id, team_id=team, workflow_name="test-run", status=status,
                                             started_at=utcnow()))
        conn.execute(insert(jobs).values(id=new_id(), team_id=team, workflow_id=workflow, run_id=ended,
                                         status="succeeded", created_at=utcnow()))
        conn.execute(insert(run_events).values(run_id=ended, seq=1, ts=utcnow(), kind="step_start"))

    def delete(*args):
        return CliRunner().invoke(app, ["server", "runs", "delete", "--config", str(config), *args])

    def count(table):
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(table)).scalar()

    assert delete(ended, "--as", "viewer").exit_code == 1
    refused = delete(ended, running, "--as", "admin")
    assert refused.exit_code == 1 and f"Run {running} is still open" in refused.output
    dry = delete(ended, "--as", "admin", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert f"Would delete run {ended} of test-run and 1 job:" in dry.output and "run_events: 1 row" in dry.output
    assert count(runs) == 2 and count(run_events) == 1
    done = delete(ended, "--as", "admin")
    assert done.exit_code == 0 and f"Deleted run {ended}" in done.output
    assert count(runs) == 1 and count(run_events) == 0 and count(jobs) == 0 and count(audit) >= 1


def test_runs_delete_takes_several_ids(tmp_path, monkeypatch):
    from sqlalchemy import func, insert

    from armature.server.db import new_id, utcnow
    from armature.server.db.schema import audit, runs, teams
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    engine = make_engine(load_config(config).database_url)
    team, ids = new_id(), [new_id() for _ in range(3)]
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="own", slug="own", created_at=utcnow()))
        for run_id in [*ids, new_id()]:
            conn.execute(insert(runs).values(id=run_id, team_id=team, workflow_name="test-run", status="succeeded",
                                             started_at=utcnow()))
    more = [new_id() for _ in range(2)]
    with engine.begin() as conn:
        for run_id in more:
            conn.execute(insert(runs).values(id=run_id, team_id=team, workflow_name="test-run", status="succeeded",
                                             started_at=utcnow()))
    done = CliRunner().invoke(app, ["server", "runs", "delete", *ids, "--as", "admin", "--config", str(config)])
    assert done.exit_code == 0, done.output
    assert all(f"Deleted run {run_id}" in done.output for run_id in ids)
    joined = CliRunner().invoke(app, ["server", "runs", "delete", f"{more[0]} ,{more[1]}", "--as", "admin",
                                      "--config", str(config)])
    assert joined.exit_code == 0, joined.output
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(runs)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(audit).where(audit.c.action == "run.delete")).scalar() == 5
