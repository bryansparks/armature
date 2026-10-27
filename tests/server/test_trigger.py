"""What started a run, as the run list shows it."""
from datetime import UTC, datetime

from sqlalchemy import delete, event, insert, select

from armature.server import trigger
from armature.server.db import new_id
from armature.server.db.schema import runner_keys, runs, teams, users, webhook_deliveries, webhooks
from armature.server.ingest import load_summary

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _world(conn):
    team = new_id()
    conn.execute(insert(teams).values(id=team, name="t", created_at=NOW))
    hook = new_id()
    conn.execute(insert(webhooks).values(id=hook, team_id=team, name="repo hooks", source="github", path_token="p",
                                         secret_name="S", created_at=NOW, updated_at=NOW))
    delivery = new_id()
    conn.execute(insert(webhook_deliveries).values(id=delivery, webhook_id=hook, received_at=NOW,
                                                   event="pull_request", status="accepted"))
    user = new_id()
    conn.execute(insert(users).values(id=user, username="ada", created_at=NOW))
    key = new_id()
    conn.execute(insert(runner_keys).values(id=key, team_id=team, name="ci-runner", key_hmac="h", created_at=NOW))
    return {"team": team, "hook": hook, "delivery": delivery, "user": user, "key": key}


def _run(conn, world, **values):
    run_id = new_id()
    conn.execute(insert(runs).values(id=run_id, team_id=world["team"], status="running", started_at=NOW,
                                     runner_key_id=world["key"], **values))
    return run_id


def _describe(conn, run_ids):
    rows = conn.execute(select(runs.c.id, *trigger.columns()).where(runs.c.id.in_(run_ids))).mappings().all()
    return trigger.describe(conn, rows)


def test_each_rule_in_order(sqlite_engine):
    with sqlite_engine.begin() as conn:
        world = _world(conn)
        cases = {
            _run(conn, world, trigger_kind="webhook",
                 trigger_ref={"webhook_id": world["hook"], "delivery_id": world["delivery"]}):
                ("repo hooks", "pull_request"),
            _run(conn, world, trigger_kind="webhook", trigger_ref={"webhook_id": world["hook"]}): ("repo hooks", None),
            _run(conn, world, trigger_kind="test", triggered_by=world["user"], actor="ada"): ("ada", "manual"),
            _run(conn, world, trigger_kind="pr", trigger_ref={"repo": "acme/app", "number": 7}, actor="ci-runner"):
                ("Pull request", "acme/app#7"),
            _run(conn, world, trigger_kind="jira", trigger_ref={"issue": "PROJ-1"}): ("Jira", "PROJ-1"),
            _run(conn, world, trigger_kind="cron", actor="ci-runner"): ("Schedule", None),
            _run(conn, world, trigger_kind="slack", actor="bea"): ("Slack", "bea"),
            _run(conn, world, trigger_kind="cli", actor="bea"): ("Command line", "bea"),
            _run(conn, world, trigger_kind="dry_run"): ("Test run", None),
            _run(conn, world, trigger_kind="nightly_sweep", actor="ci-runner"): ("Nightly Sweep", None),
            _run(conn, world): ("Unknown", None),
        }
        described = _describe(conn, list(cases))
    assert {run_id: (t["label"], t["detail"]) for run_id, t in described.items()} == cases


def test_deleted_webhook_reads_as_webhook(sqlite_engine):
    with sqlite_engine.begin() as conn:
        world = _world(conn)
        run_id = _run(conn, world, trigger_kind="webhook", trigger_ref={"webhook_id": world["hook"]})
        conn.execute(delete(webhook_deliveries))
        conn.execute(delete(webhooks))
        assert _describe(conn, [run_id])[run_id] == {"label": "Webhook", "detail": None}


def test_one_query_per_lookup_for_a_page(sqlite_engine):
    with sqlite_engine.begin() as conn:
        world = _world(conn)
        ids = [_run(conn, world, trigger_kind="webhook",
                    trigger_ref={"webhook_id": world["hook"], "delivery_id": world["delivery"]}) for _ in range(5)]
        ids += [_run(conn, world, trigger_kind="cli", triggered_by=world["user"]) for _ in range(5)]
        rows = conn.execute(select(runs.c.id, *trigger.columns()).where(runs.c.id.in_(ids))).mappings().all()
        statements = []
        listener = lambda *args: statements.append(args[2])  # noqa: E731
        event.listen(sqlite_engine, "before_cursor_execute", listener)
        try:
            described = trigger.describe(conn, rows)
        finally:
            event.remove(sqlite_engine, "before_cursor_execute", listener)
    assert len(statements) == 4
    assert {t["label"] for t in described.values()} == {"repo hooks", "ada"}


def test_summary_carries_the_trigger(sqlite_engine):
    with sqlite_engine.begin() as conn:
        world = _world(conn)
        run_id = _run(conn, world, trigger_kind="cli", actor="bea")
        assert load_summary(conn, run_id)["trigger"] == {"label": "Command line", "detail": "bea"}
