"""Poll and schedule triggers: seeding, marks, failures, double-poll prevention, cron, and the team lead's API."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import insert, select, update

from armature.server import connections, defs, pollers, vendors, worker
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    jobs,
    projects,
    teams,
    trigger_polls,
    trigger_seen,
    trigger_state,
    triggers,
)
from armature.server.db.schema import connections as connections_table
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

BODY = "name: work\nstages: []\n"
DENVER = "America/Denver"


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(vendors, "resolve_host", lambda host: ["140.82.112.5"])
    monkeypatch.setattr(pollers, "_checked", {})


@pytest.fixture
def clock():
    return Clock()


class Jira:
    """A fake Jira search that answers with the keys a test sets, recording each request."""

    def __init__(self):
        self.keys: list[str] = []
        self.status = 200
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        if request.url.path == "/graphql":
            nodes = [{"identifier": k, "title": f"t {k}", "url": f"https://linear.app/x/{k}",
                      "state": {"name": "Todo", "type": "unstarted"}, "assignee": None, "team": {"key": "ENG"}}
                     for k in self.keys]
            return httpx.Response(200, json={"data": {"issues": {"nodes": nodes}}})
        jql = parse_qs(request.url.query.decode())["jql"][0]
        only = jql.rsplit("key = ", 1)[1] if "AND key = " in jql else None
        issues = [{"key": k, "fields": {"summary": f"Do {k}", "status": {"name": "To Do"}}}
                  for k in self.keys if only is None or k == only]
        return httpx.Response(200, json={"issues": issues, "isLast": True})

    def jql(self, index: int = -1) -> str:
        return parse_qs(self.requests[index].url.query.decode())["jql"][0]


@pytest.fixture
def jira():
    return Jira()


@pytest.fixture
def world(sqlite_engine, keyring, clock):
    team, other, project, workflow, conn_id, linear_id = (new_id() for _ in range(6))
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "own", "created_at": clock()},
                                           {"id": other, "name": "other", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow, name="work", current_version=1,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", team).columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow, 1, BODY, "seed", None, clock())
        for row_id, kind, config, creds in (
                (conn_id, "jira", {"site_url": "https://acme.atlassian.net", "email": "bot@acme.test"},
                 {"api_token": "tok"}),
                (linear_id, "linear", {}, {"api_key": "lin_key"})):
            conn.execute(insert(connections_table).values(
                id=row_id, team_id=team, kind=kind, name=kind, config=config, state="ok", created_at=clock(),
                updated_at=clock(), key_id=keyring.key_id,
                sealed=keyring.seal(json.dumps(creds), connections.sealed_aad(row_id, kind), label=connections.LABEL)))
    return SimpleNamespace(team=team, other=other, project=project, workflow=workflow, jira=conn_id,
                           linear=linear_id)


def _create(engine, clock, world, kind="jira_poll", **config):
    base = {"jira_poll": {"connection_id": world.jira}, "linear_poll": {"connection_id": world.linear, "team": "ENG"},
            "schedule": {"cron": "30 6 * * *", "timezone": DENVER}}[kind]
    with engine.begin() as conn:
        return pollers.create(conn, team_id=world.team, body={"workflow_id": world.workflow, "kind": kind,
                                                              "config": {**base, **config}},
                              actor_id=None, now=clock(), ip=None)


def _run(engine, keyring, clock, trigger, jira, **kwargs):
    return pollers.run(engine, keyring, trigger.id, now=clock(), transport=httpx.MockTransport(jira), **kwargs)


def _jobs(engine):
    with engine.connect() as conn:
        return conn.execute(select(jobs).order_by(jobs.c.created_at, jobs.c.id)).all()


def _seen(engine, trigger_id):
    with engine.connect() as conn:
        return {r.item_key: r for r in conn.execute(select(trigger_seen).where(trigger_seen.c.trigger_id == trigger_id))}


def _state(engine, trigger_id):
    with engine.connect() as conn:
        return conn.execute(select(trigger_state).where(trigger_state.c.trigger_id == trigger_id)).one()


def _polls(engine, trigger_id):
    with engine.connect() as conn:
        return conn.execute(select(trigger_polls).where(trigger_polls.c.trigger_id == trigger_id)
                            .order_by(trigger_polls.c.started_at, trigger_polls.c.id)).all()


def test_first_poll_seeds_then_each_new_key_queues_once_ever(sqlite_engine, keyring, clock, world, jira):
    trigger = _create(sqlite_engine, clock, world)
    assert _state(sqlite_engine, trigger.id).next_run_at == clock()
    jira.keys = ["ENG-1", "ENG-2", "not a key"]
    result = _run(sqlite_engine, keyring, clock, trigger, jira)
    assert result["seeded"] == 2 and result["new_keys"] == [] and _jobs(sqlite_engine) == []
    assert {k: r.outcome for k, r in _seen(sqlite_engine, trigger.id).items()} == {"ENG-1": "seeded",
                                                                                  "ENG-2": "seeded"}
    assert _state(sqlite_engine, trigger.id).seeded_at == clock()
    assert jira.jql(0) == pollers.DEFAULT_JQL
    request = jira.requests[0]
    assert request.headers["host"] == "acme.atlassian.net" and request.url.path == "/rest/api/3/search/jql"

    clock.advance(minutes=5)
    jira.keys = ["ENG-1", "ENG-2", "ENG-3"]
    assert _run(sqlite_engine, keyring, clock, trigger, jira)["new_keys"] == ["ENG-3"]
    [job] = _jobs(sqlite_engine)
    assert job.inputs == {"issue": "ENG-3"} and job.trigger_id == trigger.id and job.status == "queued"
    seen = _seen(sqlite_engine, trigger.id)
    assert seen["ENG-3"].outcome == "queued" and seen["ENG-3"].job_id == job.id
    assert seen["ENG-1"].last_seen_at == clock() and seen["ENG-1"].first_seen_at < clock()

    clock.advance(minutes=5)
    assert _run(sqlite_engine, keyring, clock, trigger, jira)["new_keys"] == []
    assert len(_jobs(sqlite_engine)) == 1
    state = _state(sqlite_engine, trigger.id)
    assert state.last_status == "ok" and state.last_matched == 3 and state.last_new == 0
    assert [p.new_keys for p in _polls(sqlite_engine, trigger.id)] == [[], ["ENG-3"], []]


def test_seed_first_off_queues_everything_on_the_first_poll(sqlite_engine, keyring, clock, world, jira):
    trigger = _create(sqlite_engine, clock, world, seed_first=False, inputs={"key": "$.key", "title": "$.summary"})
    jira.keys = ["ENG-1"]
    assert _run(sqlite_engine, keyring, clock, trigger, jira)["new_keys"] == ["ENG-1"]
    assert _jobs(sqlite_engine)[0].inputs == {"key": "ENG-1", "title": "Do ENG-1"}


@pytest.mark.parametrize("status", [500, 401])
def test_a_failed_poll_writes_no_marks_and_backs_off(sqlite_engine, keyring, clock, world, jira, status):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    jira.keys, jira.status = ["ENG-1"], status
    result = _run(sqlite_engine, keyring, clock, trigger, jira)
    assert result["status"] == "error" and f"HTTP {status}" in result["error"]
    assert _seen(sqlite_engine, trigger.id) == {} and _jobs(sqlite_engine) == []
    state = _state(sqlite_engine, trigger.id)
    assert state.last_status == "error" and state.failures == 1
    assert state.next_run_at == clock() + timedelta(minutes=10)
    assert [p.status for p in _polls(sqlite_engine, trigger.id)] == ["error"]
    with sqlite_engine.connect() as conn:
        connection_state = conn.execute(select(connections_table.c.state)
                                        .where(connections_table.c.id == world.jira)).scalar()
    assert connection_state == ("error" if status == 401 else "ok")
    _run(sqlite_engine, keyring, clock, trigger, jira)
    assert _state(sqlite_engine, trigger.id).next_run_at == clock() + timedelta(minutes=20)
    jira.status = 200
    _run(sqlite_engine, keyring, clock, trigger, jira)
    assert _state(sqlite_engine, trigger.id).failures == 0 and set(_seen(sqlite_engine, trigger.id)) == {"ENG-1"}


def test_two_servers_never_poll_one_trigger_twice(sqlite_engine, keyring, clock, world):
    trigger = _create(sqlite_engine, clock, world)
    first = pollers.Scheduler(sqlite_engine, keyring, clock=clock)
    second = pollers.Scheduler(sqlite_engine, keyring, clock=clock)
    with sqlite_engine.connect() as conn:
        [row] = pollers.due(conn, clock())
    with sqlite_engine.begin() as conn:
        assert pollers.claim(conn, trigger.id, row.next_run_at, clock() + timedelta(minutes=5)) is True
    with sqlite_engine.begin() as conn:
        assert pollers.claim(conn, trigger.id, row.next_run_at, clock() + timedelta(minutes=5)) is False
    assert first.claim_due(clock()) == [] and second.claim_due(clock()) == []
    clock.advance(minutes=5)
    assert first.claim_due(clock()) == [(trigger.id, clock())]
    assert second.claim_due(clock()) == []
    assert _state(sqlite_engine, trigger.id).next_run_at == clock() + timedelta(minutes=5)


def test_the_seen_key_stops_a_second_poller_that_got_past_the_claim(sqlite_engine, keyring, clock, world):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    item = vendors.Item(key="ENG-9", title=None, url=None, fields={"key": "ENG-9"})
    assert pollers._take(sqlite_engine, trigger, None, item, clock()) == ("queued", "")
    assert pollers._take(sqlite_engine, trigger, None, item, clock()) is None
    assert len(_jobs(sqlite_engine)) == 1


def test_shadow_marks_keys_and_queues_nothing(sqlite_engine, keyring, clock, world, jira):
    trigger = _create(sqlite_engine, clock, world, seed_first=False, shadow=True)
    jira.keys = ["ENG-1"]
    assert _run(sqlite_engine, keyring, clock, trigger, jira)["new_keys"] == ["ENG-1"]
    assert _seen(sqlite_engine, trigger.id)["ENG-1"].outcome == "shadow" and _jobs(sqlite_engine) == []


def test_a_disabled_trigger_is_not_due(sqlite_engine, keyring, clock, world):
    trigger = _create(sqlite_engine, clock, world)
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == trigger.id).values(enabled=False))
    assert pollers.Scheduler(sqlite_engine, keyring, clock=clock).claim_due(clock()) == []


def test_linear_polls_a_team_filter(sqlite_engine, keyring, clock, world, jira):
    trigger = _create(sqlite_engine, clock, world, "linear_poll", states=["started", "unstarted"], assignee="me",
                      seed_first=False)
    jira.keys = ["ENG-12"]
    assert _run(sqlite_engine, keyring, clock, trigger, jira)["new_keys"] == ["ENG-12"]
    sent = json.loads(jira.requests[0].content)
    assert sent["variables"]["filter"] == {"team": {"key": {"eq": "ENG"}},
                                           "state": {"type": {"in": ["unstarted", "started"]}},
                                           "assignee": {"isMe": {"eq": True}}}
    assert jira.requests[0].headers["authorization"] == "lin_key"
    assert _jobs(sqlite_engine)[0].inputs == {"issue": "ENG-12"}


@pytest.mark.parametrize(("expr", "after", "expected"), [
    # Spring forward: 02:30 doesn't exist on March 8, so it runs an hour later, at 03:30 MDT.
    ("30 2 * * *", datetime(2026, 3, 7, 12, 0, tzinfo=UTC), datetime(2026, 3, 8, 9, 30, tzinfo=UTC)),
    ("30 2 * * *", datetime(2026, 3, 8, 9, 30, tzinfo=UTC), datetime(2026, 3, 9, 8, 30, tzinfo=UTC)),
    # Fall back: 01:30 happens twice on November 1, and runs only the first time, in MDT.
    ("30 1 * * *", datetime(2026, 10, 31, 12, 0, tzinfo=UTC), datetime(2026, 11, 1, 7, 30, tzinfo=UTC)),
    ("30 1 * * *", datetime(2026, 11, 1, 7, 30, tzinfo=UTC), datetime(2026, 11, 2, 8, 30, tzinfo=UTC)),
    # A fixed local time keeps its wall clock across the change: 06:30 is 12:30 UTC in MDT and 13:30 in MST.
    ("30 6 * * *", datetime(2026, 10, 31, 12, 31, tzinfo=UTC), datetime(2026, 11, 1, 13, 30, tzinfo=UTC)),
    ("30 6 * * 1-5", datetime(2026, 11, 6, 13, 30, tzinfo=UTC), datetime(2026, 11, 9, 13, 30, tzinfo=UTC)),
])
def test_cron_is_dst_safe(expr, after, expected):
    assert pollers.next_cron(expr, DENVER, after) == expected


def test_cron_in_utc_by_default():
    assert pollers.next_cron("0 * * * *", "UTC", datetime(2026, 3, 8, 8, 59, tzinfo=UTC)) == \
        datetime(2026, 3, 8, 9, 0, tzinfo=UTC)


def test_a_schedule_fires_once_after_missed_runs(sqlite_engine, keyring, clock, world):
    trigger = _create(sqlite_engine, clock, world, "schedule", inputs={"mode": "daily"})
    due = _state(sqlite_engine, trigger.id).next_run_at
    assert due == datetime(2026, 10, 23, 12, 30, tzinfo=UTC)
    clock.now = due + timedelta(days=3, minutes=1)
    scheduler = pollers.Scheduler(sqlite_engine, keyring, clock=clock)
    assert scheduler.claim_due(clock()) == [(trigger.id, due)]
    result = pollers.run(sqlite_engine, keyring, trigger.id, now=clock(), due=due)
    assert result["status"] == "ok"
    [job] = _jobs(sqlite_engine)
    assert job.inputs == {"mode": "daily"}
    assert [(p.status, p.error) for p in _polls(sqlite_engine, trigger.id)] == [("skipped", pollers.MISSED),
                                                                                 ("ok", None)]
    assert _state(sqlite_engine, trigger.id).next_run_at == datetime(2026, 10, 27, 12, 30, tzinfo=UTC)
    assert scheduler.claim_due(clock()) == []


def test_a_poll_run_says_which_issue_started_it(sqlite_engine, keyring, clock, world, jira):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    jira.keys = ["ENG-4"]
    _run(sqlite_engine, keyring, clock, trigger, jira)
    [job] = _jobs(sqlite_engine)
    with sqlite_engine.connect() as conn:
        found = conn.execute(select(triggers.c.config, triggers.c.webhook_id, triggers.c.kind)
                             .where(triggers.c.id == trigger.id)).first()
        assert worker._trigger_ref(conn, job, found) == ("jira", {"trigger_id": trigger.id, "issue": "ENG-4"})
    schedule = _create(sqlite_engine, clock, world, "schedule")
    with sqlite_engine.connect() as conn:
        found = conn.execute(select(triggers.c.config, triggers.c.webhook_id, triggers.c.kind)
                             .where(triggers.c.id == schedule.id)).first()
        ref = worker._trigger_ref(conn, SimpleNamespace(id="j", trigger_id=schedule.id, delivery_id=None), found)
    assert ref == ("schedule", {"trigger_id": schedule.id, "cron": "30 6 * * *"})


@pytest.mark.parametrize(("kind", "config", "message"), [
    ("jira_poll", {"interval_minutes": 0}, "interval_minutes"),
    ("jira_poll", {"max_results": 101}, "max_results"),
    ("jira_poll", {"jql": ""}, None),
    ("jira_poll", {"surprise": 1}, "Unknown keys"),
    ("jira_poll", {"inputs": {"bad-name": "$.key"}}, "not valid"),
    ("linear_poll", {"team": "eng"}, "team"),
    ("linear_poll", {"states": ["done"]}, "states"),
    ("schedule", {"cron": "61 * * * *"}, "cron"),
    ("schedule", {"cron": "* * * *"}, "cron"),
    ("schedule", {"timezone": "Mars/Base"}, "timezone"),
    ("schedule", {"cron": "* * * * *"}, "once every 5 minutes"),
    ("schedule", {"cron": "0,3 * * * *"}, "once every 5 minutes"),
    ("cron", {}, "kind"),
])
def test_bad_config_is_refused(clock, kind, config, message):
    base = {"jira_poll": {"connection_id": "c"}, "linear_poll": {"connection_id": "c", "team": "ENG"},
            "schedule": {"cron": "30 6 * * *"}, "cron": {}}[kind]
    if message is None:
        assert pollers.check_config(kind, {**base, **config}, clock())["jql"] == pollers.DEFAULT_JQL
        return
    with pytest.raises(pollers.TriggerError, match=message):
        pollers.check_config(kind, {**base, **config}, clock())


def test_a_trigger_needs_its_own_teams_connection(sqlite_engine, keyring, clock, world):
    foreign = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(connections_table).values(id=foreign, team_id=world.other, kind="jira", name="theirs",
                                                      config={}, state="ok", created_at=clock(), updated_at=clock()))
    for connection_id, message in ((foreign, "another team"), (world.linear, "needs a jira connection")):
        with pytest.raises(pollers.TriggerError, match=message):
            _create(sqlite_engine, clock, world, connection_id=connection_id)
    with pytest.raises(pollers.TriggerError, match="No such project"):
        _create(sqlite_engine, clock, world, project_id=new_id())


# The team lead's API


@pytest.fixture
def lead(sqlite_engine, keyring, clock, world, jira):
    user = add_user(sqlite_engine, keyring, clock, "lead")[0]
    add_membership(sqlite_engine, clock, user, "team", world.team, "team_lead")
    viewer = add_user(sqlite_engine, keyring, clock, "viewer")[0]
    add_membership(sqlite_engine, clock, viewer, "team", world.team, "viewer")
    client = make_client(sqlite_engine, keyring, clock)
    client.app.state.vendor_transport = httpx.MockTransport(jira)
    headers = mutation(sign_in_as(client, sqlite_engine, keyring, clock, user, step_up=True))
    return SimpleNamespace(client=client, headers=headers, viewer=viewer)


def _post(lead, path, body=None):
    return lead.client.post(path, json=body or {}, headers=lead.headers)


def test_lead_creates_lists_and_edits_a_trigger(sqlite_engine, lead, world, clock):
    response = _post(lead, f"/api/teams/{world.team}/triggers", {
        "workflow_id": world.workflow, "kind": "jira_poll",
        "config": {"name": "My sprint", "connection_id": world.jira, "interval_minutes": 10}})
    assert response.status_code == 200, response.text
    trigger = response.json()["trigger"]
    assert trigger["config"]["jql"] == pollers.DEFAULT_JQL and trigger["connection_name"] == "jira"
    assert trigger["state"]["next_run_at"] is not None and trigger["picked_up_24h"] == 0
    listed = lead.client.get(f"/api/teams/{world.team}/triggers").json()
    assert [t["id"] for t in listed["triggers"]] == [trigger["id"]]
    assert {c["kind"] for c in listed["connections"]} == {"jira", "linear"}
    assert listed["default_jql"] == pollers.DEFAULT_JQL
    edited = _post(lead, f"/api/triggers/{trigger['id']}", {"config": {"jql": "project = ENG"}, "enabled": False})
    assert edited.json()["trigger"]["config"]["jql"] == "project = ENG"
    assert edited.json()["trigger"]["config"]["interval_minutes"] == 10
    assert edited.json()["trigger"]["enabled"] is False
    with sqlite_engine.connect() as conn:
        actions = conn.execute(select(audit.c.action).where(audit.c.target_id == trigger["id"])).scalars().all()
    assert sorted(actions) == ["trigger.create", "trigger.update"]


def test_other_roles_and_teams_are_refused(sqlite_engine, keyring, lead, world, clock):
    trigger = _create(sqlite_engine, clock, world)
    viewer = make_client(sqlite_engine, keyring, clock)
    headers = mutation(sign_in_as(viewer, sqlite_engine, keyring, clock, lead.viewer, step_up=True))
    assert viewer.get(f"/api/teams/{world.team}/triggers").status_code == 403
    assert viewer.post(f"/api/triggers/{trigger.id}/preview", json={}, headers=headers).status_code == 403
    assert lead.client.get(f"/api/teams/{world.other}/triggers").status_code in (403, 404)
    assert lead.client.get(f"/api/triggers/{new_id()}").status_code == 404


def test_preview_shows_keys_and_which_are_seen_and_writes_nothing(sqlite_engine, keyring, lead, world, jira, clock):
    trigger = _create(sqlite_engine, clock, world)
    jira.keys = ["ENG-1"]
    _run(sqlite_engine, keyring, clock, trigger, jira)
    jira.keys = ["ENG-1", "ENG-2"]
    items = _post(lead, f"/api/triggers/{trigger.id}/preview").json()["items"]
    assert [(i["key"], i["seen"]["outcome"] if i["seen"] else None) for i in items] == [("ENG-1", "seeded"),
                                                                                         ("ENG-2", None)]
    assert set(_seen(sqlite_engine, trigger.id)) == {"ENG-1"}
    again = _post(lead, f"/api/triggers/{trigger.id}/preview")
    assert again.status_code == 429


def test_why_a_key_was_or_was_not_picked_up(sqlite_engine, keyring, lead, world, jira, clock):
    trigger = _create(sqlite_engine, clock, world)
    jira.keys = ["ENG-1"]
    _run(sqlite_engine, keyring, clock, trigger, jira)
    jira.keys = ["ENG-1", "ENG-2"]

    def why(key):
        clock.advance(seconds=11)
        response = _post(lead, f"/api/triggers/{trigger.id}/why", {"key": key})
        assert response.status_code == 200, response.text
        return response.json()

    seen = why("eng-1")
    assert seen["verdict"] == "seen" and seen["seen"]["outcome"] == "seeded" and seen["returned"] is True
    assert jira.jql() == ("(assignee = currentUser() AND sprint in openSprints() AND statusCategory != Done) "
                          "AND key = ENG-1")
    waiting = why("ENG-2")
    assert waiting["verdict"] == "next_poll" and waiting["seen"] is None
    missing = why("ENG-3")
    assert missing["verdict"] == "not_returned" and "openSprints()" in missing["message"]
    clock.advance(seconds=11)
    assert _post(lead, f"/api/triggers/{trigger.id}/why", {"key": "nope"}).status_code == 400


def test_queue_it_anyway_requeues_a_seen_key_and_is_audited(sqlite_engine, keyring, lead, world, jira, clock):
    trigger = _create(sqlite_engine, clock, world)
    jira.keys = ["ENG-1"]
    _run(sqlite_engine, keyring, clock, trigger, jira)
    response = _post(lead, f"/api/triggers/{trigger.id}/requeue", {"key": "ENG-1"})
    assert response.status_code == 200, response.text
    [job] = _jobs(sqlite_engine)
    assert response.json()["job_id"] == job.id and job.inputs == {"issue": "ENG-1"}
    seen = _seen(sqlite_engine, trigger.id)["ENG-1"]
    assert seen.outcome == "queued" and seen.job_id == job.id
    clock.advance(seconds=11)
    assert _post(lead, f"/api/triggers/{trigger.id}/requeue", {"key": "ENG-7"}).status_code == 409
    listed = lead.client.get(f"/api/triggers/{trigger.id}/seen?outcome=queued&q=eng").json()["seen"]
    assert [(s["key"], s["job_status"]) for s in listed] == [("ENG-1", "queued")]
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(audit.c.detail).where(audit.c.action == "trigger.requeue")).scalar() == {
            "key": "ENG-1", "job_id": job.id, "previous": "seeded"}


def test_run_now_seed_and_reset(sqlite_engine, keyring, lead, world, jira, clock):
    trigger = _create(sqlite_engine, clock, world)
    jira.keys = ["ENG-1", "ENG-2"]
    assert _post(lead, f"/api/triggers/{trigger.id}/seed").json() == {"matched": 2, "seeded": 2}
    clock.advance(seconds=11)
    jira.keys = ["ENG-1", "ENG-2", "ENG-3"]
    poll = _post(lead, f"/api/triggers/{trigger.id}/run").json()["poll"]
    assert poll["new_keys"] == ["ENG-3"] and len(_jobs(sqlite_engine)) == 1
    assert _state(sqlite_engine, trigger.id).next_run_at == clock() + timedelta(minutes=5)
    polls = lead.client.get(f"/api/triggers/{trigger.id}/polls").json()["polls"]
    assert polls[0]["new_keys"] == ["ENG-3"]
    assert _post(lead, f"/api/triggers/{trigger.id}/reset").json() == {"forgot": 3}
    assert _seen(sqlite_engine, trigger.id) == {} and _state(sqlite_engine, trigger.id).seeded_at is None


def test_delete_drops_poll_state_and_keeps_jobs(sqlite_engine, keyring, lead, world, jira, clock):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    jira.keys = ["ENG-1"]
    _run(sqlite_engine, keyring, clock, trigger, jira)
    assert _post(lead, f"/api/triggers/{trigger.id}/delete").json() == {"ok": True}
    with sqlite_engine.connect() as conn:
        for table in (trigger_state, trigger_seen, trigger_polls):
            assert conn.execute(select(table)).all() == []
    [job] = _jobs(sqlite_engine)
    assert job.trigger_id is None


def test_deleting_the_workflow_drops_its_triggers(sqlite_engine, keyring, world, jira, clock):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    jira.keys = ["ENG-1"]
    _run(sqlite_engine, keyring, clock, trigger, jira)
    with sqlite_engine.begin() as conn:
        conn.execute(update(jobs).values(status="succeeded"))
        row = defs.get_row(conn, defs.WORKFLOW, world.workflow)
        defs.remove(conn, defs.WORKFLOW, row, access=SimpleNamespace(is_admin=True), actor_id=None, ip=None)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(triggers)).all() == [] and conn.execute(select(trigger_seen)).all() == []


def test_a_schedule_every_five_minutes_is_allowed(clock):
    assert pollers.check_config("schedule", {"cron": "*/5 * * * *"}, clock())["cron"] == "*/5 * * * *"


def test_a_newer_schedule_run_supersedes_one_still_queued(sqlite_engine, keyring, clock, world):
    trigger = _create(sqlite_engine, clock, world, "schedule")
    first = pollers.run(sqlite_engine, keyring, trigger.id, now=clock())["job_id"]
    clock.advance(days=1)
    second = pollers.run(sqlite_engine, keyring, trigger.id, now=clock())["job_id"]
    rows = {r.id: r for r in _jobs(sqlite_engine)}
    assert rows[first].status == "superseded" and rows[first].superseded_by == second
    assert rows[second].status == "queued" and rows[second].concurrency_key == f"schedule:{trigger.id}"


def test_seeding_commits_its_marks_and_state_together(sqlite_engine, keyring, clock, world, jira, monkeypatch):
    trigger = _create(sqlite_engine, clock, world)
    jira.keys = ["ENG-1"]
    monkeypatch.setattr(pollers, "_record", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        _run(sqlite_engine, keyring, clock, trigger, jira)
    state = _state(sqlite_engine, trigger.id)
    assert set(_seen(sqlite_engine, trigger.id)) == {"ENG-1"} and state.seeded_at == clock()
    assert state.seeded_count == 1


def test_a_poll_stops_reading_past_the_body_cap(sqlite_engine, keyring, clock, world, monkeypatch):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    monkeypatch.setattr(vendors, "POLL_BODY_MAX", 64)
    monkeypatch.setattr(vendors.jira, "POLL_BODY_MAX", 64)
    chunks = []

    def big(request):
        def body():
            for _ in range(1000):
                chunks.append(1)
                yield b'{"issues": [' + b" " * 50
        return httpx.Response(200, content=body())

    result = pollers.run(sqlite_engine, keyring, trigger.id, now=clock(), transport=httpx.MockTransport(big))
    assert result["status"] == "error" and "more than" in result["error"]
    assert len(chunks) < 10 and _seen(sqlite_engine, trigger.id) == {}


def test_a_trigger_disabled_after_its_claim_does_not_run(sqlite_engine, keyring, clock, world, jira):
    trigger = _create(sqlite_engine, clock, world, seed_first=False)
    with sqlite_engine.begin() as conn:
        conn.execute(update(triggers).where(triggers.c.id == trigger.id).values(enabled=False))
    jira.keys = ["ENG-1"]
    assert _run(sqlite_engine, keyring, clock, trigger, jira) is None
    assert jira.requests == [] and _polls(sqlite_engine, trigger.id) == []
