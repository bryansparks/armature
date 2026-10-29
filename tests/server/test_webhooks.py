import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import insert, select

from armature.server import secrets as secret_store
from armature.server import webhooks as hooks
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    jobs,
    projects,
    runs,
    teams,
    triggers,
    webhook_deliveries,
    webhooks,
    workflows,
)
from tests.server.app_support import (
    ORIGIN,
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

SECRET = "s3cret-value"


def _sig(secret, raw):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _github(raw, event="pull_request", delivery="d-1", secret=SECRET):
    return {"X-Hub-Signature-256": "sha256=" + _sig(secret, raw), "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}


def _slack(raw, clock, secret=SECRET, **extra):
    stamp = str(int(clock().timestamp()))
    signature = "v0=" + _sig(secret, b"v0:" + stamp.encode() + b":" + raw)
    return {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": signature, "Content-Type": "application/json",
            **extra}


def _body(value):
    return json.dumps(value).encode()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(sqlite_engine, keyring, clock):
    team, other, project = new_id(), new_id(), new_id()
    wf, wf2, wf_other, wf_instance = new_id(), new_id(), new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="mobile", created_at=clock()))
        conn.execute(insert(teams).values(id=other, name="web", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", repo="Acme/App",
                                             created_at=clock()))
        for workflow_id, team_id, name in ((wf, team, "code-review"), (wf2, team, "pr-fix"),
                                           (wf_other, other, "theirs"), (wf_instance, None, "shared")):
            conn.execute(insert(workflows).values(id=workflow_id, team_id=team_id, name=name, current_version=3,
                                                  created_at=clock(), updated_at=clock()))
    lead = add_user(sqlite_engine, keyring, clock, "lead")[0]
    add_membership(sqlite_engine, clock, lead, "team", team, "team_lead")
    users = {"lead": lead}
    for role in ("contributor", "viewer"):
        users[role] = add_user(sqlite_engine, keyring, clock, role)[0]
        add_membership(sqlite_engine, clock, users[role], "team", team, role)
    users["other_lead"] = add_user(sqlite_engine, keyring, clock, "otherlead")[0]
    add_membership(sqlite_engine, clock, users["other_lead"], "team", other, "team_lead")
    return SimpleNamespace(team=team, other=other, project=project, wf=wf, wf2=wf2, wf_other=wf_other,
                           wf_instance=wf_instance, users=users)


@pytest.fixture
def app(sqlite_engine, keyring, clock):
    return make_client(sqlite_engine, keyring, clock)


def _as(app, sqlite_engine, keyring, clock, user_id, step_up=True):
    return mutation(sign_in_as(app, sqlite_engine, keyring, clock, user_id, step_up=step_up))


@pytest.fixture
def lead(app, sqlite_engine, keyring, clock, world):
    headers = _as(app, sqlite_engine, keyring, clock, world.users["lead"])
    return SimpleNamespace(client=app, headers=headers)


def _create(lead, world, source="github", **body):
    response = lead.client.post(f"/api/teams/{world.team}/webhooks",
                                json={"name": f"{source}-hook", "source": source, **body}, headers=lead.headers)
    assert response.status_code == 200, response.text
    return response.json()["webhook"]


def _set_secret(lead, hook, value=SECRET):
    response = lead.client.post(f"/api/webhooks/{hook['id']}/secret", json={"value": value}, headers=lead.headers)
    assert response.status_code == 200, response.text
    return response.json()


def _rule(lead, hook, workflow_id, **config):
    response = lead.client.post(f"/api/webhooks/{hook['id']}/rules",
                                json={"workflow_id": workflow_id, "config": config}, headers=lead.headers)
    assert response.status_code == 200, response.text
    return response.json()["rule"]


def _path(hook):
    return hook["url"].removeprefix(ORIGIN)


def _approve(lead, hook, **values):
    with lead.client.app.state.server.engine.begin() as conn:
        conn.execute(webhooks.update().where(webhooks.c.id == hook["id"]).values(approval="approved", **values))


def _hook(lead, world, source="github", **body):
    hook = _create(lead, world, source, **body)
    _set_secret(lead, hook)
    _approve(lead, hook)
    return hook


def _deliveries(engine, hook_id=None):
    with engine.connect() as conn:
        query = select(webhook_deliveries).order_by(webhook_deliveries.c.received_at, webhook_deliveries.c.id)
        if hook_id:
            query = query.where(webhook_deliveries.c.webhook_id == hook_id)
        return conn.execute(query).all()


def _jobs(engine):
    with engine.connect() as conn:
        return conn.execute(select(jobs).order_by(jobs.c.created_at, jobs.c.id)).all()


# verify, unit level

def test_verify_github(clock):
    raw = b'{"a":1}'
    assert hooks.verify("github", {}, SECRET, _github(raw), raw, clock()) is None
    assert hooks.verify("github", {}, SECRET, _github(raw, secret="wrong"), raw, clock()) == "bad signature"
    assert hooks.verify("github", {}, SECRET, {}, raw, clock()) == "bad signature"
    assert hooks.verify("github", {}, None, _github(raw), raw, clock()) == "secret not set"


def test_verify_slack_checks_signature_and_clock(clock):
    raw = b'{"type":"event_callback"}'
    headers = _slack(raw, clock)
    assert hooks.verify("slack", {}, SECRET, headers, raw, clock()) is None
    assert hooks.verify("slack", {}, SECRET, {**headers, "X-Slack-Signature": "v0=00"}, raw, clock()) == "bad signature"
    assert hooks.verify("slack", {}, SECRET, headers, raw, clock() + timedelta(seconds=301)) \
        == "stale or missing timestamp"
    assert hooks.verify("slack", {}, SECRET, headers, raw, clock() - timedelta(seconds=299)) is None


def test_verify_generic_hmac_and_bearer(clock):
    raw = b"{}"
    cfg = {"generic_auth": "hmac", "generic_signature_header": None}
    assert hooks.verify("generic", cfg, SECRET, {"X-Signature-256": _sig(SECRET, raw)}, raw, clock()) is None
    assert hooks.verify("generic", cfg, SECRET, {"x-signature-256": "sha256=" + _sig(SECRET, raw)}, raw,
                        clock()) is None
    assert hooks.verify("generic", cfg, SECRET, {"X-Signature-256": _sig("x", raw)}, raw, clock()) == "bad signature"
    custom = {"generic_auth": "hmac", "generic_signature_header": "X-Ci-Sig"}
    assert hooks.verify("generic", custom, SECRET, {"X-Ci-Sig": _sig(SECRET, raw)}, raw, clock()) is None
    bearer = {"generic_auth": "bearer"}
    assert hooks.verify("generic", bearer, SECRET, {"Authorization": f"Bearer {SECRET}"}, raw, clock()) is None
    assert hooks.verify("generic", bearer, SECRET, {"Authorization": "Bearer nope"}, raw, clock()) == "bad signature"
    assert hooks.verify("generic", bearer, SECRET, {"Authorization": f"Basic {SECRET}"}, raw, clock()) \
        == "bad signature"


def test_event_and_key():
    slack_cb = {"type": "event_callback", "event": {"type": "app_mention"}, "event_id": "Ev1"}
    assert hooks.event_of("slack", {}, slack_cb) == "app_mention"
    assert hooks.event_of("slack", {}, {"type": "block_actions"}) == "block_actions"
    assert hooks.event_of("github", {"X-GitHub-Event": "push"}, {}) == "push"
    assert hooks.event_of("generic", {}, {"type": "x"}) is None
    assert hooks.delivery_key("slack", {}, {}, slack_cb) == "Ev1"
    assert hooks.delivery_key("generic", {"generic_delivery_header": "X-Id"}, {"x-id": "7"}, {}) == "7"
    assert hooks.delivery_key("generic", {}, {"x-id": "7"}, {}) is None


def test_lookup_paths():
    payload = {"a": {"b": [{"c": 1}, {"c": 2}]}, "x": None}
    assert hooks.lookup(payload, "a.b.1.c") == 2
    assert hooks.lookup(payload, "$.a.b.0.c") == 1
    assert hooks.lookup(payload, "$") == payload
    assert hooks.lookup(payload, "x") is None
    assert hooks.lookup(payload, "a.b.5.c") is hooks._MISSING
    assert hooks.lookup(payload, "a.zz") is hooks._MISSING


# receiver

def test_github_delivery_queues_a_job_pinned_to_the_version(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf, events=["pull_request"], inputs={"pr": "$.number", "repo": "$.repo", "mode": "fast"})
    raw = _body({"number": 7, "repo": "acme/app"})
    response = lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert response.json() == {"status": "accepted", "jobs": 1}
    [job] = _jobs(sqlite_engine)
    assert (job.workflow_id, job.workflow_version, job.status) == (world.wf, 3, "queued")
    assert job.inputs == {"pr": 7, "repo": "acme/app", "mode": "fast"}
    [delivery] = _deliveries(sqlite_engine)
    assert job.delivery_id == delivery.id and job.trigger_id is not None
    assert delivery.status == "accepted" and delivery.event == "pull_request" and delivery.delivery_key == "d-1"
    assert delivery.payload == raw.decode()
    assert delivery.matched == [{"trigger_id": job.trigger_id, "workflow_id": world.wf, "job_id": job.id,
                                 "shadow": False}]
    assert "x-hub-signature-256" not in delivery.headers and delivery.headers["x-github-event"] == "pull_request"


def _mark(sqlite_engine, keyring, clock, world, head):
    from armature.server import runners
    from armature.server.db.schema import review_marks
    with sqlite_engine.begin() as conn:
        row, _ = runners.open_run(conn, keyring, team_id=world.team, project_id=world.project, workflow_id=world.wf,
                                  workflow_name="code-review", now=clock())
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo="acme/app", pr_number=7, head_sha=head, run_id=row["id"],
                                                 conclusion="success", created_at=clock()))


@pytest.mark.parametrize("kind,queued", [("review", 0), ("fix", 1)])
def test_a_review_of_a_head_that_carries_a_mark_is_not_queued(sqlite_engine, keyring, clock, lead, world, kind,
                                                              queued):
    head = "a" * 40
    with sqlite_engine.begin() as conn:
        conn.execute(workflows.update().where(workflows.c.id == world.wf).values(kind=kind))
    _mark(sqlite_engine, keyring, clock, world, head)
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf, events=["pull_request"], project_from="repository.full_name")
    raw = _body({"repository": {"full_name": "Acme/App"}, "pull_request": {"head": {"sha": head}}})
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).json()["jobs"] == queued
    [delivery] = _deliveries(sqlite_engine)
    assert ("skipped" in delivery.matched[0]) is (queued == 0)
    if not queued:
        assert delivery.matched[0]["skipped"] == f"head {head[:12]} already carries an Armature review mark"
        raw = _body({"repository": {"full_name": "Acme/App"}, "pull_request": {"head": {"sha": "b" * 40}}})
        response = lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery="d-2"))
        assert response.json()["jobs"] == 1


def test_github_ping_pongs(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    raw = _body({"zen": "hi"})
    response = lead.client.post(_path(hook), content=raw, headers=_github(raw, event="ping"))
    assert response.json() == {"status": "pong"}
    assert _deliveries(sqlite_engine)[0].reason == "ping"


def test_bad_signature_is_rejected_without_payload(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf)
    raw = _body({"number": 1})
    response = lead.client.post(_path(hook), content=raw, headers=_github(raw, secret="wrong"))
    assert response.status_code == 401
    [delivery] = _deliveries(sqlite_engine)
    assert (delivery.status, delivery.payload, delivery.delivery_key) == ("rejected", None, None)
    assert delivery.reason == "bad signature"
    assert _jobs(sqlite_engine) == []


def test_missing_secret_is_rejected(sqlite_engine, lead, world):
    hook = _create(lead, world)
    raw = b"{}"
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).status_code == 401
    assert _deliveries(sqlite_engine)[0].reason == "secret not set"


def test_unknown_and_disabled_tokens_are_404(lead, world):
    assert lead.client.post("/hooks/nope", content=b"{}").json() == {"error": "not found"}
    hook = _hook(lead, world)
    lead.client.post(f"/api/webhooks/{hook['id']}", json={"enabled": False}, headers=lead.headers)
    raw = b"{}"
    response = lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert response.status_code == 404 and response.json() == {"error": "not found"}


def test_duplicate_delivery_is_not_queued_twice(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf)
    raw = _body({"n": 1})
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).json()["jobs"] == 1
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).json() == {"status": "duplicate"}
    assert len(_jobs(sqlite_engine)) == 1
    assert sorted(d.status for d in _deliveries(sqlite_engine)) == ["accepted", "duplicate"]


def test_slack_url_verification_echoes_the_challenge(lead, world, clock):
    hook = _hook(lead, world, "slack")
    raw = _body({"type": "url_verification", "challenge": "abc123"})
    response = lead.client.post(_path(hook), content=raw, headers=_slack(raw, clock))
    assert response.status_code == 200 and response.text == "abc123"
    assert response.headers["content-type"].startswith("text/plain")
    unsigned = lead.client.post(_path(hook), content=raw, headers={"Content-Type": "application/json"})
    assert unsigned.status_code == 401 and "abc123" not in unsigned.text


def test_slack_retry_is_deduplicated_by_event_id(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world, "slack")
    _rule(lead, hook, world.wf)
    raw = _body({"type": "event_callback", "event_id": "Ev9", "event": {"type": "app_mention"}})
    retry = {"X-Slack-Retry-Num": "1"}
    assert lead.client.post(_path(hook), content=raw, headers=_slack(raw, clock, **retry)).json() \
        == {"status": "accepted", "jobs": 1}
    assert lead.client.post(_path(hook), content=raw, headers=_slack(raw, clock, **retry)).json() \
        == {"status": "duplicate"}
    assert len(_jobs(sqlite_engine)) == 1


def test_slack_stale_timestamp_is_rejected(lead, world, clock):
    hook = _hook(lead, world, "slack")
    raw = _body({"type": "event_callback", "event": {"type": "message"}})
    headers = _slack(raw, clock)
    clock.advance(seconds=301)
    assert lead.client.post(_path(hook), content=raw, headers=headers).status_code == 401


def test_slack_event_callback_matches_on_inner_type(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world, "slack")
    _rule(lead, hook, world.wf, events=["app_mention"], inputs={"text": "$.event.text"})
    raw = _body({"type": "event_callback", "event_id": "Ev1", "event": {"type": "app_mention", "text": "hi"}})
    assert lead.client.post(_path(hook), content=raw, headers=_slack(raw, clock)).json()["jobs"] == 1
    assert _jobs(sqlite_engine)[0].inputs == {"text": "hi"}


def test_a_rule_thread_template_sets_the_job_thread(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world, "slack")
    _rule(lead, hook, world.wf, events=["message"],
          thread="slack:{{ event.channel }}:{{ event.thread_ts or event.ts }}")
    for event_id, extra in (("Ev1", {}), ("Ev2", {"thread_ts": "1700.1"})):
        raw = _body({"type": "event_callback", "event_id": event_id,
                     "event": {"type": "message", "channel": "C9", "ts": "1700.5", **extra}})
        assert lead.client.post(_path(hook), content=raw, headers=_slack(raw, clock)).json()["jobs"] == 1
    assert sorted(job.thread for job in _jobs(sqlite_engine)) == ["slack:C9:1700.1", "slack:C9:1700.5"]


def test_cancel_only_needs_a_key_and_never_a_conversation():
    assert hooks.check_rule({"key": ["pull_request.number"], "cancel_only": True})["cancel_only"] is True
    assert "cancel_only" not in hooks.check_rule({})
    for config, message in (({"cancel_only": True}, "needs a key"), ({"cancel_only": "yes"}, "true or false"),
                            ({"key": ["a"], "cancel_only": True, "conversation": True}, "conversation")):
        with pytest.raises(hooks.WebhookError, match=message):
            hooks.check_rule(config)


def test_a_rule_dry_flag_is_a_boolean_and_defaults_off():
    assert hooks.check_rule({})["dry"] is False and hooks.check_rule({"dry": True})["dry"] is True
    with pytest.raises(hooks.WebhookError, match="dry must be true or false"):
        hooks.check_rule({"dry": "yes"})


def test_a_rule_thread_must_be_a_template_and_renders_to_none_when_empty():
    with pytest.raises(hooks.WebhookError, match="thread"):
        hooks.check_rule({"thread": "{{ broken"})
    with pytest.raises(hooks.WebhookError, match="thread"):
        hooks.check_rule({"thread": 7})
    assert hooks.check_rule({"thread": "gh:{{ repo }}"})["thread"] == "gh:{{ repo }}"
    assert hooks.render_thread("{{ missing }}", {}) is None
    assert hooks.render_thread("x" * 192, {}) is None
    assert hooks.render_thread("{{ ''.__class__ }}", {}) is None


def test_generic_bearer_and_delivery_header(sqlite_engine, lead, world):
    hook = _hook(lead, world, "generic", generic_auth="bearer", generic_delivery_header="X-Build-Id")
    _rule(lead, hook, world.wf, event_from="kind", events=["build"], inputs={"all": "$"})
    raw = _body({"kind": "build", "id": 4})
    headers = {"Authorization": f"Bearer {SECRET}", "X-Build-Id": "b4", "Content-Type": "application/json"}
    assert lead.client.post(_path(hook), content=raw, headers=headers).json() == {"status": "accepted", "jobs": 1}
    assert lead.client.post(_path(hook), content=raw, headers=headers).json() == {"status": "duplicate"}
    assert _jobs(sqlite_engine)[0].inputs == {"all": {"kind": "build", "id": 4}}
    delivery = _deliveries(sqlite_engine)[0]
    assert "authorization" not in delivery.headers and delivery.headers["x-build-id"] == "b4"


def test_generic_hmac(lead, world):
    hook = _hook(lead, world, "generic")
    raw = b'{"x":1}'
    ok = lead.client.post(_path(hook), content=raw, headers={"X-Signature-256": "sha256=" + _sig(SECRET, raw)})
    assert ok.json() == {"status": "ignored", "jobs": 0}
    assert lead.client.post(_path(hook), content=raw, headers={"X-Signature-256": "00"}).status_code == 401


def test_form_encoded_payload(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world, "slack")
    _rule(lead, hook, world.wf, events=["block_actions"], inputs={"user": "$.user.id"})
    raw = b"payload=" + json.dumps({"type": "block_actions", "user": {"id": "U1"}}).encode().replace(b" ", b"+")
    headers = {**_slack(raw, clock), "Content-Type": "application/x-www-form-urlencoded"}
    assert lead.client.post(_path(hook), content=raw, headers=headers).json()["jobs"] == 1
    assert _jobs(sqlite_engine)[0].inputs == {"user": "U1"}


# rules

def _decide(sqlite_engine, world, rules, payload, event="push", source="github"):
    rows = [SimpleNamespace(id=f"r{i}", workflow_id=world.wf, enabled=rule.pop("enabled", True),
                            config=hooks.check_rule(rule), created_at=i) for i, rule in enumerate(rules)]
    with sqlite_engine.connect() as conn:
        return hooks.evaluate(conn, world.team, source, rows, event, payload)


@pytest.mark.parametrize("op,value,payload,expected", [
    ("eq", "open", {"s": "open"}, True), ("eq", "open", {"s": "closed"}, False), ("eq", "open", {}, False),
    ("ne", "open", {"s": "closed"}, True), ("ne", "open", {"s": "open"}, False), ("ne", "open", {}, True),
    ("in", ["a", "b"], {"s": "b"}, True), ("in", ["a"], {"s": "c"}, False),
    ("not_in", ["a"], {"s": "c"}, True), ("not_in", ["a"], {"s": "a"}, False),
    ("starts_with", "fix", {"s": "  fix: crash"}, True), ("starts_with", "fix", {"s": "a fix"}, False),
    ("starts_with", "Fix", {"s": "fix"}, False), ("starts_with", "1", {"s": 1}, False),
    ("contains", "bug", {"s": "a bug here"}, True), ("contains", "BUG", {"s": "a bug"}, False),
    ("contains", "x", {}, False),
    ("exists", None, {"s": None}, True), ("exists", None, {}, False),
    ("missing", None, {}, True), ("missing", None, {"s": 1}, False),
])
def test_condition_ops(sqlite_engine, world, op, value, payload, expected):
    condition = {"path": "s", "op": op, **({"value": value} if value is not None else {})}
    [decision] = _decide(sqlite_engine, world, [{"conditions": [condition]}], payload)
    assert decision["matched"] is expected
    if not expected:
        assert decision["reason"].startswith("condition 1 failed")


def test_events_filter(sqlite_engine, world):
    [decision] = _decide(sqlite_engine, world, [{"events": ["pull_request"]}], {}, event="push")
    assert not decision["matched"] and "push" in decision["reason"]
    assert _decide(sqlite_engine, world, [{"events": []}], {}, event="push")[0]["matched"]


def test_project_from_matches_repo_case_insensitively(sqlite_engine, world):
    [decision] = _decide(sqlite_engine, world, [{"project_from": "repository.full_name"}],
                         {"repository": {"full_name": "acme/app"}})
    assert decision["matched"] and decision["project_id"] == world.project
    [miss] = _decide(sqlite_engine, world, [{"project_from": "repository.full_name"}],
                     {"repository": {"full_name": "acme/other"}})
    assert not miss["matched"] and "acme/other" in miss["reason"]


def test_project_from_ignores_other_teams(sqlite_engine, world):
    with sqlite_engine.connect() as conn:
        assert hooks.project_for_repo(conn, world.other, "acme/app") is None


@pytest.mark.parametrize("op,value,subject,expected", [
    ("starts_with", "FIX", " fix: x", True), ("contains", "Bug", "a BUG", True), ("contains", "bug", "a b", False),
])
def test_text_ops_ignore_case(sqlite_engine, world, op, value, subject, expected):
    condition = {"path": "s", "op": op, "value": value, "ignore_case": True}
    [decision] = _decide(sqlite_engine, world, [{"conditions": [condition]}], {"s": subject})
    assert decision["matched"] is expected


def test_shadow_stop_does_not_stop(sqlite_engine, world):
    decisions = _decide(sqlite_engine, world, [{"order": 1, "stop": True, "shadow": True}, {"order": 2}], {})
    assert [d["matched"] for d in decisions] == [True, True]


def _scoped_workflow(sqlite_engine, world, clock, project_id, team_id=None):
    workflow_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(workflows).values(id=workflow_id, team_id=team_id or world.team, project_id=project_id,
                                              name=f"scoped-{workflow_id[:8]}", current_version=1,
                                              created_at=clock(), updated_at=clock()))
    return workflow_id


def _decide_for(sqlite_engine, world, workflow_id, config, payload):
    row = SimpleNamespace(id="r0", workflow_id=workflow_id, enabled=True, config=hooks.check_rule(config),
                          created_at=0)
    with sqlite_engine.connect() as conn:
        return hooks.evaluate(conn, world.team, "github", [row], "push", payload)[0]


def test_project_scoped_workflow_defaults_the_job_project(sqlite_engine, world, clock):
    workflow_id = _scoped_workflow(sqlite_engine, world, clock, world.project)
    assert _decide_for(sqlite_engine, world, workflow_id, {}, {})["project_id"] == world.project
    matched = _decide_for(sqlite_engine, world, workflow_id, {"project_from": "repo"}, {"repo": "acme/app"})
    assert matched["matched"] and matched["project_id"] == world.project


def test_project_from_must_agree_with_a_scoped_workflow(sqlite_engine, world, clock):
    other_project = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(projects).values(id=other_project, team_id=world.team, name="lib", repo="acme/lib",
                                             created_at=clock()))
    workflow_id = _scoped_workflow(sqlite_engine, world, clock, world.project)
    decision = _decide_for(sqlite_engine, world, workflow_id, {"project_from": "repo"}, {"repo": "acme/lib"})
    assert not decision["matched"] and "not the project" in decision["reason"]


def test_missing_or_foreign_workflow_does_not_match(sqlite_engine, world):
    gone = _decide_for(sqlite_engine, world, new_id(), {}, {})
    assert not gone["matched"] and "no longer exists" in gone["reason"]
    foreign = _decide_for(sqlite_engine, world, world.wf_other, {}, {})
    assert not foreign["matched"] and "not visible" in foreign["reason"]


def test_order_and_stop(sqlite_engine, world):
    decisions = _decide(sqlite_engine, world, [{"order": 2}, {"order": 1, "stop": True}, {"order": 3}], {})
    assert [d["trigger_id"] for d in decisions] == ["r1", "r0", "r2"]
    assert [d["matched"] for d in decisions] == [True, False, False]
    assert decisions[1]["reason"].startswith("skipped")


def test_disabled_rule_does_not_match(sqlite_engine, world):
    [decision] = _decide(sqlite_engine, world, [{"enabled": False}], {})
    assert not decision["matched"] and decision["reason"] == "rule is disabled"


def test_shadow_records_without_a_job_and_fan_out_queues_two(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf)
    _rule(lead, hook, world.wf2)
    _rule(lead, hook, world.wf_instance, shadow=True)
    raw = _body({"n": 1})
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).json() == {"status": "accepted", "jobs": 2}
    assert {j.workflow_id for j in _jobs(sqlite_engine)} == {world.wf, world.wf2}
    matched = _deliveries(sqlite_engine)[0].matched
    shadow = next(m for m in matched if m["shadow"])
    assert shadow["workflow_id"] == world.wf_instance and shadow["job_id"] is None


def test_shadow_only_queues_nothing(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf, shadow=True)
    raw = _body({"n": 1})
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).json() == {"status": "accepted", "jobs": 0}
    assert _jobs(sqlite_engine) == []


@pytest.mark.parametrize("config,message", [
    ({"conditions": [{"path": "a", "op": "like", "value": 1}]}, "op must be"),
    ({"conditions": [{"path": "a", "op": "matches", "value": "x"}]}, "op must be"),
    ({"conditions": [{"path": "a", "op": "contains", "value": "x" * 201}]}, "at most 200"),
    ({"conditions": [{"path": "a", "op": "contains", "value": 1}]}, "must be text"),
    ({"conditions": [{"path": "a", "op": "eq", "value": 1, "ignore_case": True}]}, "ignore_case"),
    ({"conditions": [{"path": "a", "op": "eq"}]}, "needs a value"),
    ({"conditions": [{"path": "a", "op": "in", "value": "x"}]}, "must be a list"),
    ({"conditions": [{"path": "a b", "op": "exists"}]}, "dotted path"),
    ({"conditions": [{"path": "a", "op": "exists", "extra": 1}]}, "unknown keys"),
    ({"inputs": {"bad-name": "$.a"}}, "not valid"),
    ({"inputs": {"ok": "$bad"}}, "must be $"),
    ({"surprise": True}, "Unknown rule keys"),
    ({"order": "1"}, "order must be"),
    ({"secrets": ["not a name"]}, "secret names"),
])
def test_invalid_rule_config_is_refused(lead, world, config, message):
    hook = _hook(lead, world)
    response = lead.client.post(f"/api/webhooks/{hook['id']}/rules", json={"workflow_id": world.wf, "config": config},
                                headers=lead.headers)
    assert response.status_code == 400 and message in response.json()["error"]


def test_rule_needs_a_workflow_the_team_sees(lead, world):
    hook = _hook(lead, world)
    response = lead.client.post(f"/api/webhooks/{hook['id']}/rules", json={"workflow_id": world.wf_other, "config": {}},
                                headers=lead.headers)
    assert response.status_code == 400
    assert _rule(lead, hook, world.wf_instance)["workflow_name"] == "shared"


def test_rule_mapping_text_accepts_yaml(lead, world):
    hook = _hook(lead, world)
    mapping = "conditions:\n  - {path: action, op: eq, value: opened}\ninputs:\n  pr: $.number\n"
    response = lead.client.post(f"/api/webhooks/{hook['id']}/rules",
                                json={"workflow_id": world.wf, "config": {"order": 1}, "mapping": mapping},
                                headers=lead.headers)
    config = response.json()["rule"]["config"]
    assert config["conditions"] == [{"path": "action", "op": "eq", "value": "opened"}]
    assert config["inputs"] == {"pr": "$.number"} and config["order"] == 1
    bad = lead.client.post(f"/api/webhooks/{hook['id']}/rules",
                           json={"workflow_id": world.wf, "mapping": "events: [x]"}, headers=lead.headers)
    assert bad.status_code == 400


def test_rule_update_and_delete(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf)
    raw = _body({"n": 1})
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    updated = lead.client.post(f"/api/rules/{rule['id']}", json={"enabled": False, "workflow_id": world.wf2},
                               headers=lead.headers).json()["rule"]
    assert updated["enabled"] is False and updated["workflow_name"] == "pr-fix"
    assert lead.client.post(f"/api/rules/{rule['id']}/delete", json={}, headers=lead.headers).json() == {"ok": True}
    [job] = _jobs(sqlite_engine)
    assert job.trigger_id is None
    assert lead.client.post(f"/api/rules/{rule['id']}", json={}, headers=lead.headers).status_code == 404


# admin API

def test_webhook_json_has_the_url_and_never_the_secret(lead, world):
    hook = _create(lead, world)
    assert hook["url"].startswith(f"{ORIGIN}/hooks/") and len(hook["url"].rsplit("/", 1)[1]) >= 32
    assert hook["secret_set"] is False and hook["secret_name"] == "WEBHOOK_GITHUB_HOOK"
    _set_secret(lead, hook)
    listed = lead.client.get(f"/api/teams/{world.team}/webhooks")
    assert listed.json()["webhooks"][0]["secret_set"] is True
    detail = lead.client.get(f"/api/webhooks/{hook['id']}")
    assert SECRET not in listed.text and SECRET not in detail.text
    assert {w["name"] for w in detail.json()["workflows"]} == {"code-review", "pr-fix", "shared"}


def test_generated_secret_is_shown_once(sqlite_engine, keyring, lead, world):
    hook = _create(lead, world)
    response = lead.client.post(f"/api/webhooks/{hook['id']}/secret", json={}, headers=lead.headers).json()
    generated = response["secret"]
    assert generated and response["webhook"]["secret_set"] is True
    with sqlite_engine.connect() as conn:
        stored = secret_store.resolve_for_run(conn, keyring, world.team, [hook["secret_name"]])
    assert stored == {hook["secret_name"]: generated}
    assert generated not in lead.client.get(f"/api/webhooks/{hook['id']}").text
    assert _set_secret(lead, hook, "pasted")["secret"] is None


def test_name_is_unique_per_team(lead, world):
    _create(lead, world)
    again = lead.client.post(f"/api/teams/{world.team}/webhooks", json={"name": "github-hook", "source": "github"},
                             headers=lead.headers)
    assert again.status_code == 409


def test_create_refuses_bad_settings(lead, world):
    url = f"/api/teams/{world.team}/webhooks"
    for body in ({"name": "a", "source": "gitlab"},
                 {"name": "b", "source": "generic", "generic_auth": "basic"},
                 {"name": "d", "source": "generic", "generic_delivery_header": "Authorization"},
                 {"name": "e", "source": "github", "secret_name": "no spaces"}):
        assert lead.client.post(url, json=body, headers=lead.headers).status_code == 400, body


def test_step_up_needed_for_create_delete_secret_and_forward(app, sqlite_engine, keyring, clock, world):
    headers = _as(app, sqlite_engine, keyring, clock, world.users["lead"], step_up=False)
    url = f"/api/teams/{world.team}/webhooks"
    assert app.post(url, json={"name": "h", "source": "github"}, headers=headers).json()["error"] == "step_up_required"
    with sqlite_engine.begin() as conn:
        hook = hooks.create(conn, team_id=world.team, name="h", body={"source": "github"}, actor_id=None,
                            now=clock(), ip=None)
    base = f"/api/webhooks/{hook.id}"
    assert app.post(f"{base}/secret", json={}, headers=headers).status_code == 403
    assert app.post(f"{base}/delete", json={}, headers=headers).status_code == 403
    assert app.post(base, json={"forward_url": "https://x.test/"}, headers=headers).status_code == 403
    assert app.post(base, json={"secret_name": "OTHER"}, headers=headers).status_code == 403
    assert app.post(base, json={"enabled": False, "forward_url": None}, headers=headers).status_code == 200
    assert app.post(f"{base}/rules", json={"workflow_id": world.wf, "config": {}}, headers=headers).status_code == 200


@pytest.mark.parametrize("role", ["contributor", "viewer"])
def test_lower_roles_are_refused(app, sqlite_engine, keyring, clock, world, role, lead):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf)
    headers = _as(app, sqlite_engine, keyring, clock, world.users[role])
    assert app.get(f"/api/teams/{world.team}/webhooks").status_code == 403
    assert app.get(f"/api/webhooks/{hook['id']}").status_code == 403
    assert app.post(f"/api/teams/{world.team}/webhooks", json={"name": "z", "source": "github"},
                    headers=headers).status_code == 403
    assert app.post(f"/api/rules/{rule['id']}/delete", json={}, headers=headers).status_code == 403
    assert app.get(f"/api/webhooks/{hook['id']}/deliveries").status_code == 403


def test_other_team_lead_sees_404(app, sqlite_engine, keyring, clock, world, lead):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf)
    headers = _as(app, sqlite_engine, keyring, clock, world.users["other_lead"])
    assert app.get(f"/api/webhooks/{hook['id']}").status_code == 404
    assert app.get(f"/api/teams/{world.team}/webhooks").status_code == 404
    assert app.post(f"/api/rules/{rule['id']}", json={"enabled": False}, headers=headers).status_code == 404
    assert app.post(f"/api/webhooks/{hook['id']}/delete", json={}, headers=headers).status_code == 404
    other_hook = app.post(f"/api/teams/{world.other}/webhooks", json={"name": "o", "source": "github"},
                          headers=headers).json()["webhook"]
    refused = app.post(f"/api/webhooks/{other_hook['id']}/rules", json={"workflow_id": world.wf, "config": {}},
                       headers=headers)
    assert refused.status_code == 400


def test_writes_are_audited(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf)
    lead.client.post(f"/api/rules/{rule['id']}", json={"enabled": False}, headers=lead.headers)
    lead.client.post(f"/api/webhooks/{hook['id']}", json={"name": "renamed"}, headers=lead.headers)
    lead.client.post(f"/api/rules/{rule['id']}/delete", json={}, headers=lead.headers)
    lead.client.post(f"/api/webhooks/{hook['id']}/delete", json={}, headers=lead.headers)
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(audit.c.action, audit.c.actor_id, audit.c.detail)
                            .where(audit.c.action.like("webhook.%"))).all()
    assert [r.action for r in rows] == ["webhook.create", "webhook.secret", "webhook.rule_create",
                                        "webhook.rule_update", "webhook.update", "webhook.rule_delete",
                                        "webhook.delete"]
    assert {r.actor_id for r in rows} == {world.users["lead"]}
    assert all(SECRET not in json.dumps(r.detail) for r in rows)


def test_deliveries_list_detail_and_test(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf, conditions=[{"path": "action", "op": "eq", "value": "opened"}])
    raw = _body({"action": "closed"})
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    base = f"/api/webhooks/{hook['id']}/deliveries"
    [listed] = lead.client.get(base).json()["deliveries"]
    assert listed["status"] == "ignored" and "payload" not in listed
    detail = lead.client.get(f"{base}/{listed['id']}").json()["delivery"]
    assert detail["payload"] == raw.decode() and detail["headers"]["x-github-event"] == "pull_request"
    lead.client.post(f"/api/rules/{rule['id']}", json={"config": {"conditions": []}}, headers=lead.headers)
    [decision] = lead.client.post(f"{base}/{listed['id']}/test", json={}, headers=lead.headers).json()["decisions"]
    assert decision["matched"] is True and decision["trigger_id"] == rule["id"]
    assert _jobs(sqlite_engine) == []


def test_deliveries_page_newest_first(lead, world, clock):
    hook = _hook(lead, world)
    for n in range(3):
        raw = _body({"n": n})
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}"))
        clock.advance(seconds=1)
    base = f"/api/webhooks/{hook['id']}/deliveries"
    page = lead.client.get(f"{base}?limit=2").json()
    assert [d["delivery_key"] for d in page["deliveries"]] == ["d2", "d1"]
    rest = lead.client.get(base, params={"limit": 2, "before": page["next_before"]}).json()
    assert [d["delivery_key"] for d in rest["deliveries"]] == ["d0"]


def test_matched_shows_the_run_once_the_job_has_one(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf)
    raw = _body({"n": 1})
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [job] = _jobs(sqlite_engine)
    run_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, status="running", started_at=clock()))
        conn.execute(jobs.update().where(jobs.c.id == job.id).values(run_id=run_id, status="running"))
    [delivery] = lead.client.get(f"/api/webhooks/{hook['id']}/deliveries").json()["deliveries"]
    assert delivery["matched"][0]["run_id"] == run_id and delivery["matched"][0]["job_status"] == "running"


# forward

class _Recorder:
    def __init__(self, fail=False):
        self.requests = []
        self.fail = fail

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("refused")
        return httpx.Response(202)


@pytest.fixture
def admin(sqlite_engine, keyring, clock):
    client = make_client(sqlite_engine, keyring, clock)
    user_id = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)[0]
    return SimpleNamespace(client=client, headers=_as(client, sqlite_engine, keyring, clock, user_id))


def test_forward_url_is_admin_only(lead, admin, world):
    url = f"/api/teams/{world.team}/webhooks"
    refused = lead.client.post(url, json={"name": "f", "source": "github", "forward_url": "https://gw.test/"},
                               headers=lead.headers)
    assert refused.status_code == 403 and "admin" in refused.json()["error"]
    bad = admin.client.post(url, json={"name": "g", "source": "github", "forward_url": "ftp://x"}, headers=admin.headers)
    assert bad.status_code == 400
    hook = _hook(admin, world, forward_url="https://gw.test/hook")
    base = f"/api/webhooks/{hook['id']}"
    assert lead.client.get(base).json()["webhook"]["forward_url"] == "https://gw.test/hook"
    assert lead.client.post(base, json={"forward_url": None}, headers=lead.headers).status_code == 403
    same = lead.client.post(base, json={"forward_url": "https://gw.test/hook", "enabled": False}, headers=lead.headers)
    assert same.status_code == 200
    assert admin.client.post(base, json={"forward_url": None}, headers=admin.headers).json()["webhook"]["forward_url"] \
        is None


def _patch_httpx(monkeypatch, recorder):
    real = httpx.Client
    monkeypatch.setattr(hooks.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(recorder), **kw))


def test_forward_reposts_the_original_bytes_and_signature(sqlite_engine, lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _hook(admin, world)
    step = admin.client.post(f"/api/webhooks/{hook['id']}", json={"forward_url": "http://127.0.0.1:9/gw"},
                             headers=admin.headers)
    assert step.status_code == 200
    raw = _body({"n": 1})
    proxy = {"X-Forwarded-For": "10.0.0.1", "X-Forwarded-Proto": "https", "X-Real-IP": "10.0.0.1",
             "Forwarded": "for=10.0.0.1", "Cookie": "a=b"}
    response = lead.client.post(_path(hook), content=raw, headers={**_github(raw), **proxy})
    assert response.json()["status"] == "ignored"
    [sent] = recorder.requests
    assert str(sent.url) == "http://127.0.0.1:9/gw" and sent.content == raw
    assert sent.headers["x-hub-signature-256"] == _github(raw)["X-Hub-Signature-256"]
    assert sent.headers["host"] == "127.0.0.1:9"
    assert not {name.lower() for name in proxy} & {name.lower() for name in sent.headers}
    assert _deliveries(sqlite_engine)[0].forward_status == "ok 202"
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert len(recorder.requests) == 1


def test_forward_failure_does_not_change_the_response(sqlite_engine, lead, admin, world, monkeypatch):
    _patch_httpx(monkeypatch, _Recorder(fail=True))
    hook = _hook(admin, world, forward_url="https://gw.test/hook")
    _rule(lead, hook, world.wf)
    raw = _body({"n": 1})
    response = lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert response.json() == {"status": "accepted", "jobs": 1}
    assert _deliveries(sqlite_engine)[0].forward_status == "error: ConnectError"


def test_rejected_delivery_is_not_forwarded(lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _hook(admin, world, forward_url="https://gw.test/hook")
    raw = b"{}"
    lead.client.post(_path(hook), content=raw, headers=_github(raw, secret="bad"))
    assert recorder.requests == []


# deletes and pruning

def test_workflow_delete_clears_triggers_and_jobs(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf)
    raw = _body({"n": 1})
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    with sqlite_engine.begin() as conn:
        conn.execute(jobs.update().values(status="succeeded"))
        conn.execute(insert(jobs).values(id=new_id(), team_id=world.team, workflow_id=world.wf2, status="succeeded",
                                         attempts=0, trigger_id=rule["id"], created_at=lead.client.app.state.server.now()))
    response = lead.client.post(f"/api/workflows/{world.wf}/delete", json={}, headers=lead.headers)
    assert response.status_code == 200, response.text
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(triggers)).all() == []
        assert [(j.workflow_id, j.trigger_id) for j in conn.execute(select(jobs))] == [(world.wf2, None)]


def test_webhook_delete_removes_rules_and_deliveries_and_keeps_jobs(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf)
    raw = _body({"n": 1})
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert lead.client.post(f"/api/webhooks/{hook['id']}/delete", json={}, headers=lead.headers).json() == {"ok": True}
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(webhooks)).all() == []
        assert conn.execute(select(triggers)).all() == []
        assert conn.execute(select(webhook_deliveries)).all() == []
        [job] = conn.execute(select(jobs)).all()
    assert job.trigger_id is None and job.delivery_id is not None
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).status_code == 404


def test_prune_drops_deliveries_older_than_two_weeks(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world)
    raw = _body({"n": 1})
    lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery="old"))
    clock.advance(days=15)
    with sqlite_engine.begin() as conn:
        assert hooks.prune(conn, clock()) == 1
    lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery="new"))
    assert [d.delivery_key for d in _deliveries(sqlite_engine)] == ["new"]


def test_receiver_prunes_at_most_hourly(sqlite_engine, lead, world, clock):
    hooks._pruned_at.clear()
    hook = _hook(lead, world)
    raw = _body({"n": 1})

    def post(key):
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=key))
        return sorted(d.delivery_key for d in _deliveries(sqlite_engine))

    post("a")
    clock.advance(days=13, hours=23, minutes=30)
    assert post("b") == ["a", "b"]
    clock.advance(minutes=40)
    assert post("c") == ["a", "b", "c"]
    clock.advance(minutes=30)
    assert post("d") == ["b", "c", "d"]


def test_receiver_is_rate_limited(lead, world, monkeypatch):
    from armature.server.app import routes_webhooks
    monkeypatch.setattr(routes_webhooks, "IP_LIMIT", 2)
    hook = _hook(lead, world)
    raw = b"{}"
    codes = [lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=str(n))).status_code
             for n in range(3)]
    assert codes == [200, 200, 429]


def test_failed_verification_is_charged_separately(sqlite_engine, lead, world, monkeypatch):
    from armature.server.app import routes_webhooks
    monkeypatch.setattr(routes_webhooks, "BAD_LIMIT", 2)
    monkeypatch.setattr(routes_webhooks, "WEBHOOK_LIMIT", 2)
    hook = _hook(lead, world)
    raw = b"{}"
    bad = [lead.client.post(_path(hook), content=raw, headers=_github(raw, secret="x")).status_code for _ in range(3)]
    assert bad == [401, 401, 429]
    assert len(_deliveries(sqlite_engine)) == 2
    good = [lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=str(n))).status_code
            for n in range(3)]
    assert good == [200, 200, 429]


def test_prune_failure_is_logged_and_retried(sqlite_engine, monkeypatch, clock, caplog):
    hooks._pruned_at.clear()

    def broken(conn, now):
        raise RuntimeError("disk full")

    monkeypatch.setattr(hooks, "prune", broken)
    hooks.maybe_prune(sqlite_engine, clock())
    assert str(sqlite_engine.url) not in hooks._pruned_at
    assert "pruning webhook deliveries failed" in caplog.text
    assert hooks.prune_due(sqlite_engine, clock())


def test_integrity_error_that_is_not_a_duplicate_is_raised(sqlite_engine, lead, world, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    hook = _hook(lead, world, "generic", generic_auth="bearer")

    def broken(*args, **kwargs):
        raise IntegrityError("insert", None, Exception("fk"))

    monkeypatch.setattr(hooks, "enqueue", broken)
    with pytest.raises(IntegrityError):
        lead.client.post(_path(hook), content=b"{}", headers={"Authorization": f"Bearer {SECRET}"})


def test_rule_secrets_need_a_step_up(app, sqlite_engine, keyring, clock, world):
    headers = _as(app, sqlite_engine, keyring, clock, world.users["lead"], step_up=False)
    with sqlite_engine.begin() as conn:
        hook = hooks.create(conn, team_id=world.team, name="h", body={"source": "github"}, actor_id=None,
                            now=clock(), ip=None)
    rules = f"/api/webhooks/{hook.id}/rules"
    refused = app.post(rules, json={"workflow_id": world.wf, "config": {"secrets": ["GH_TOKEN"]}}, headers=headers)
    assert refused.json()["error"] == "step_up_required"
    rule = app.post(rules, json={"workflow_id": world.wf, "config": {"order": 1}}, headers=headers).json()["rule"]
    path = f"/api/rules/{rule['id']}"
    assert app.post(path, json={"config": {"secrets": ["GH_TOKEN"]}}, headers=headers).status_code == 403
    assert app.post(path, json={"config": {"order": 2}}, headers=headers).status_code == 200
    with sqlite_engine.begin() as conn:
        conn.execute(triggers.update().where(triggers.c.id == rule["id"])
                     .values(config=hooks.check_rule({"secrets": ["GH_TOKEN"]})))
    kept = app.post(path, json={"config": {"order": 3, "secrets": ["GH_TOKEN"]}}, headers=headers)
    assert kept.status_code == 200
    assert app.post(path, json={"enabled": False}, headers=headers).status_code == 200


# Jira and Linear

def _jira(raw, delivery="j-1", secret=SECRET):
    return {"X-Hub-Signature": "sha256=" + _sig(secret, raw), "X-Atlassian-Webhook-Identifier": delivery,
            "Content-Type": "application/json"}


def _linear(raw, delivery="l-1", secret=SECRET):
    return {"Linear-Signature": _sig(secret, raw), "Linear-Delivery": delivery, "Linear-Event": "Issue",
            "Content-Type": "application/json"}


def _linear_body(clock, **fields):
    return _body({"type": "Issue", "action": "create", "organizationId": "org-1",
                  "webhookTimestamp": int(clock().timestamp() * 1000), **fields})


def test_verify_jira_matches_the_vendor_test_vector(clock):
    secret, raw = "It's a Secret to Everybody", b"Hello World!"
    signed = {"X-Hub-Signature": "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"}
    assert hooks.verify("jira", {}, secret, signed, raw, clock()) is None
    assert hooks.verify("jira", {}, SECRET, signed, raw, clock()) == "bad signature"
    sha1 = {"X-Hub-Signature": "sha1=" + hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()}
    assert hooks.verify("jira", {}, secret, sha1, raw, clock()) == "bad signature"


def test_verify_linear_checks_signature_then_timestamp(clock):
    raw = _linear_body(clock)
    assert hooks.verify("linear", {}, SECRET, _linear(raw), raw, clock()) is None
    assert hooks.verify("linear", {}, SECRET, _linear(raw, secret="x"), raw, clock()) == "bad signature"
    assert hooks.verify("linear", {}, SECRET, _linear(raw), raw, clock() + timedelta(seconds=61)) \
        == "stale or missing timestamp"
    assert hooks.verify("linear", {}, SECRET, _linear(raw, secret="x"), raw, clock() + timedelta(seconds=61)) \
        == "bad signature"
    assert hooks.verify("linear", {}, SECRET, _linear(b"nope"), b"nope", clock()) == "stale or missing timestamp"


def test_jira_and_linear_event_and_key():
    assert hooks.event_of("jira", {}, {"webhookEvent": "jira:issue_updated"}) == "jira:issue_updated"
    assert hooks.delivery_key("jira", {}, {"X-Atlassian-Webhook-Identifier": "j9"}, {}) == "j9"
    assert hooks.event_of("linear", {"Linear-Event": "Comment"}, {"type": "Issue"}) == "Issue"
    assert hooks.event_of("linear", {"Linear-Event": "Comment"}, {}) == "Comment"
    assert hooks.delivery_key("linear", {}, {"Linear-Delivery": "u-1"}, {}) == "u-1"


@pytest.mark.parametrize("source,payload,sender", [
    ("github", {"repository": {"id": 12}, "installation": {"id": 3}}, "github:installation:3"),
    ("github", {"repository": {"id": 12}}, "github:repo:12"),
    ("github", {"repository": {"id": True}}, None),
    ("jira", {"issue": {"self": "https://Acme.atlassian.net/rest/api/2/issue/1"}}, "jira:https://acme.atlassian.net"),
    ("jira", {"comment": {"self": "https://b.atlassian.net/rest/api/2/comment/1"}}, "jira:https://b.atlassian.net"),
    ("jira", {"issue": {"self": "javascript:alert(1)"}}, None),
    ("jira", {"issue": {"self": "https://api.atlassian.com/ex/jira/c0ffee-1/rest/api/2/issue/1"}},
     "jira:cloud:c0ffee-1"),
    ("linear", {"organizationId": "o-1"}, "linear:org:o-1"),
    ("slack", {"team_id": "T1"}, "slack:team:T1"),
    ("slack", {"team": {"id": "T2"}}, "slack:team:T2"),
    ("generic", {"team_id": "T1", "repository": {"id": 1}}, None),
    ("github", ["not", "a", "dict"], None),
])
def test_sender_of(source, payload, sender):
    assert hooks.sender_of(source, payload) == sender


def test_jira_delivery_queues_a_job(sqlite_engine, lead, world):
    hook = _hook(lead, world, "jira")
    _rule(lead, hook, world.wf, events=["jira:issue_updated"], inputs={"key": "$.issue.key"})
    raw = _body({"webhookEvent": "jira:issue_updated",
                 "issue": {"key": "MEM-1", "self": "https://acme.atlassian.net/rest/api/2/issue/1"}})
    retry = {"X-Atlassian-Webhook-Retry": "1"}
    assert lead.client.post(_path(hook), content=raw, headers=_jira(raw)).json() == {"status": "accepted", "jobs": 1}
    assert lead.client.post(_path(hook), content=raw, headers={**_jira(raw), **retry}).json() == {"status": "duplicate"}
    assert _jobs(sqlite_engine)[0].inputs == {"key": "MEM-1"}
    [first] = [d for d in _deliveries(sqlite_engine) if d.status == "accepted"]
    assert (first.event, first.delivery_key) == ("jira:issue_updated", "j-1")
    assert first.sender == "jira:https://acme.atlassian.net" and "x-hub-signature" not in first.headers


def test_linear_delivery_queues_a_job_and_stale_is_rejected(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world, "linear")
    _rule(lead, hook, world.wf, events=["Issue"], conditions=[{"path": "action", "op": "eq", "value": "create"}])
    raw = _linear_body(clock)
    assert lead.client.post(_path(hook), content=raw, headers=_linear(raw)).json() == {"status": "accepted", "jobs": 1}
    delivery = _deliveries(sqlite_engine)[0]
    assert (delivery.delivery_key, delivery.sender) == ("l-1", "linear:org:org-1")
    assert "linear-signature" not in delivery.headers and delivery.headers["linear-event"] == "Issue"
    clock.advance(seconds=61)
    assert lead.client.post(_path(hook), content=raw, headers=_linear(raw, delivery="l-2")).status_code == 401


# approval

def _repo(repo=1, **fields):
    return _body({"repository": {"id": repo, "full_name": f"acme/r{repo}"}, **fields})


def _held(engine, hook_id):
    return [d for d in _deliveries(engine, hook_id) if d.status == "held"]


def _hook_row(engine, hook_id):
    with engine.connect() as conn:
        return conn.execute(select(webhooks).where(webhooks.c.id == hook_id)).first()


def test_new_webhook_is_pending_and_holds_verified_deliveries(sqlite_engine, lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _create(admin, world, approval="approved", forward_url="https://gw.test/hook")
    assert hook["approval"] == "pending" and hook["pinned_senders"] is None
    _set_secret(admin, hook)
    _rule(lead, hook, world.wf)
    raw = _repo()
    response = lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert response.status_code == 200 and response.json() == {"status": "held"}
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).json() == {"status": "duplicate"}
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw, secret="x")).status_code == 401
    [held] = _held(sqlite_engine, hook["id"])
    assert (held.reason, held.sender, held.delivery_key) == ("webhook is pending approval", "github:repo:1", "d-1")
    assert _jobs(sqlite_engine) == [] and recorder.requests == []
    lead.client.post(f"/api/webhooks/{hook['id']}", json={"enabled": False}, headers=lead.headers)
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery="d-2")).status_code == 404


def test_approve_pins_replays_and_forwards_once(sqlite_engine, lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _create(admin, world, forward_url="https://gw.test/hook")
    _set_secret(admin, hook)
    _rule(lead, hook, world.wf, inputs={"repo": "$.repository.full_name"})
    raw = _repo()
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery="d-2"))
    first, second = _held(sqlite_engine, hook["id"])
    approved = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{first.id}/approve", json={},
                                headers=lead.headers).json()
    assert (approved["replayed"], approved["status"], approved["jobs"]) == (True, "accepted", 1)
    assert approved["webhook"]["approval"] == "approved" and approved["webhook"]["pinned_senders"] == ["github:repo:1"]
    [job] = _jobs(sqlite_engine)
    assert job.delivery_id == first.id and job.inputs == {"repo": "acme/r1"}
    assert [d.id for d in _held(sqlite_engine, hook["id"])] == [second.id]
    [sent] = recorder.requests
    assert sent.content == raw and sent.headers["x-hub-signature-256"] == _github(raw)["X-Hub-Signature-256"]
    again = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{first.id}/approve", json={}, headers=lead.headers)
    assert again.status_code == 409 and len(recorder.requests) == 1
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery="d-3")).json()["jobs"] == 1
    assert len(_jobs(sqlite_engine)) == 2


def test_approve_without_a_stored_payload_replays_nothing(sqlite_engine, lead, world, monkeypatch):
    monkeypatch.setattr(hooks, "PAYLOAD_MAX", 10)
    hook = _create(lead, world)
    _set_secret(lead, hook)
    _rule(lead, hook, world.wf)
    raw = _repo()
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [held] = _held(sqlite_engine, hook["id"])
    result = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held.id}/approve", json={},
                              headers=lead.headers).json()
    assert (result["replayed"], result["jobs"], result["webhook"]["pinned_senders"]) == (False, 0, ["github:repo:1"])
    assert _jobs(sqlite_engine) == [] and _held(sqlite_engine, hook["id"]) == []


def test_approve_and_enable_need_a_secret(sqlite_engine, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    raw = _repo()
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [held] = _held(sqlite_engine, hook["id"])
    with sqlite_engine.begin() as conn:
        secret_store.delete_secret(conn, team_id=world.team, name=hook["secret_name"], actor_id=None)
    refused = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held.id}/approve", json={},
                               headers=lead.headers)
    assert refused.status_code == 409 and "secret" in refused.json()["error"]
    assert _hook_row(sqlite_engine, hook["id"]).approval == "pending"
    base = f"/api/webhooks/{hook['id']}"
    assert lead.client.post(base, json={"enabled": False}, headers=lead.headers).status_code == 200
    assert lead.client.post(base, json={"enabled": True}, headers=lead.headers).status_code == 409
    _set_secret(lead, hook)
    assert lead.client.post(base, json={"enabled": True}, headers=lead.headers).json()["webhook"]["enabled"] is True


def test_pinning_off_admits_any_signed_sender_and_never_pins(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _rule(lead, hook, world.wf)
    for n, raw in enumerate((_repo(1), _repo(2), _body({"n": 1}))):
        assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}")).json()["jobs"] == 1
    assert _hook_row(sqlite_engine, hook["id"]).pinned_senders is None


def test_multi_repo_webhook_pins_a_set_and_approving_adds_to_it(sqlite_engine, lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _hook(admin, world, forward_url="https://gw.test/hook")
    _approve(lead, hook, pinned_senders=["github:repo:1", "github:repo:2"])
    _rule(lead, hook, world.wf)
    for n in (1, 2):
        raw = _repo(n)
        assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}")).json()["jobs"] == 1
    third = _repo(3)
    assert lead.client.post(_path(hook), content=third, headers=_github(third, delivery="d3")).json() \
        == {"status": "held"}
    [held] = _held(sqlite_engine, hook["id"])
    assert held.reason == "sender github:repo:3 is not pinned" and len(recorder.requests) == 2
    base = f"/api/webhooks/{hook['id']}/deliveries/{held.id}"
    assert lead.client.post(f"{base}/replay", json={}, headers=lead.headers).status_code == 409
    approved = lead.client.post(f"{base}/approve", json={}, headers=lead.headers).json()
    assert approved["jobs"] == 1 and len(recorder.requests) == 3
    assert approved["webhook"]["pinned_senders"] == ["github:repo:1", "github:repo:2", "github:repo:3"]
    for n in (1, 2, 3):
        raw = _repo(n, again=True)
        assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"e{n}")).json()["jobs"] == 1


def test_replay_needs_an_approved_webhook_and_a_matching_held_delivery(sqlite_engine, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    _rule(lead, hook, world.wf)
    for n in range(2):
        raw = _repo(1, n=n)
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}"))
    first, second = _held(sqlite_engine, hook["id"])
    base = f"/api/webhooks/{hook['id']}/deliveries"
    pending = lead.client.post(f"{base}/{second.id}/replay", json={}, headers=lead.headers)
    assert pending.status_code == 409 and "Approve" in pending.json()["error"]
    lead.client.post(f"{base}/{first.id}/approve", json={}, headers=lead.headers)
    replayed = lead.client.post(f"{base}/{second.id}/replay", json={}, headers=lead.headers).json()
    assert (replayed["replayed"], replayed["status"], replayed["jobs"]) == (True, "accepted", 1)
    assert lead.client.post(f"{base}/{second.id}/replay", json={}, headers=lead.headers).status_code == 409
    assert lead.client.post(f"{base}/nope/replay", json={}, headers=lead.headers).status_code == 404
    assert len(_jobs(sqlite_engine)) == 2


def test_reject_deletes_the_webhook_and_its_secret(sqlite_engine, keyring, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    raw = _repo()
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    assert lead.client.post(f"/api/webhooks/{hook['id']}/reject", json={}, headers=lead.headers).json() == {"ok": True}
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(webhooks)).all() == []
        assert conn.execute(select(webhook_deliveries)).all() == []
        assert secret_store.list_secrets(conn, world.team) == []
        actions = [r.action for r in conn.execute(select(audit.c.action).where(audit.c.action.like("webhook.%")))]
    assert actions[-1] == "webhook.reject" and "webhook.delete" not in actions
    assert lead.client.post(_path(hook), content=raw, headers=_github(raw)).status_code == 404


def test_reject_keeps_a_shared_or_instance_secret(sqlite_engine, keyring, lead, world):
    shared = _create(lead, world, secret_name="SHARED")
    _create(lead, world, "slack", secret_name="SHARED")
    _set_secret(lead, shared)
    lead.client.post(f"/api/webhooks/{shared['id']}/reject", json={}, headers=lead.headers)
    instance = _create(lead, world, "jira", secret_name="INSTANCE_ONE")
    with sqlite_engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=None, name="INSTANCE_ONE", value="v", actor_id=None)
    lead.client.post(f"/api/webhooks/{instance['id']}/reject", json={}, headers=lead.headers)
    with sqlite_engine.connect() as conn:
        assert [s["name"] for s in secret_store.list_secrets(conn, world.team)] == ["SHARED"]
        assert [s["name"] for s in secret_store.list_secrets(conn, None)] == ["INSTANCE_ONE"]


def test_approve_replay_and_reject_need_a_lead_with_step_up(app, sqlite_engine, keyring, clock, world, lead):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    raw = _repo()
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [held] = _held(sqlite_engine, hook["id"])
    paths = [f"/api/webhooks/{hook['id']}/deliveries/{held.id}/{action}" for action in ("approve", "replay", "dismiss")]
    paths.append(f"/api/webhooks/{hook['id']}/reject")
    no_step_up = _as(app, sqlite_engine, keyring, clock, world.users["lead"], step_up=False)
    assert [app.post(p, json={}, headers=no_step_up).status_code for p in paths] == [403] * 4
    for role in ("contributor", "viewer"):
        headers = _as(app, sqlite_engine, keyring, clock, world.users[role])
        assert [app.post(p, json={}, headers=headers).status_code for p in paths] == [403] * 4
    other = _as(app, sqlite_engine, keyring, clock, world.users["other_lead"])
    assert [app.post(p, json={}, headers=other).status_code for p in paths] == [404] * 4
    assert _hook_row(sqlite_engine, hook["id"]).approval == "pending" and _held(sqlite_engine, hook["id"])


def test_approval_writes_are_audited(sqlite_engine, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    for n in range(2):
        raw = _repo(1, n=n)
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}"))
    first, second = _held(sqlite_engine, hook["id"])
    base = f"/api/webhooks/{hook['id']}/deliveries"
    lead.client.post(f"{base}/{first.id}/approve", json={}, headers=lead.headers)
    lead.client.post(f"{base}/{second.id}/replay", json={}, headers=lead.headers)
    with sqlite_engine.connect() as conn:
        rows = conn.execute(select(audit.c.action, audit.c.detail).where(audit.c.action.in_(
            ["webhook.approve", "webhook.replay"]))).all()
    assert [r.action for r in rows] == ["webhook.approve", "webhook.replay"]
    assert rows[0].detail["pinned_senders"] == ["github:repo:1"] and rows[1].detail["delivery_id"] == second.id


# review fixes

def test_pending_or_mismatched_deliveries_are_never_forwarded(sqlite_engine, lead, admin, world, clock, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _create(admin, world, forward_url="https://gw.test/hook")
    _set_secret(admin, hook)
    ping = _repo(1)
    assert lead.client.post(_path(hook), content=ping, headers=_github(ping, event="ping")).json() == {"status": "pong"}
    bad = b"not json"
    assert lead.client.post(_path(hook), content=bad, headers=_github(bad, delivery="d-2")).status_code == 400
    slack = _create(admin, world, "slack", forward_url="https://gw.test/slack")
    _set_secret(admin, slack)
    challenge = _body({"type": "url_verification", "challenge": "c1"})
    assert lead.client.post(_path(slack), content=challenge, headers=_slack(challenge, clock)).text == "c1"
    assert recorder.requests == []
    _approve(lead, hook, pinned_senders=["github:repo:1"])
    other = _repo(2)
    lead.client.post(_path(hook), content=other, headers=_github(other, event="ping", delivery="d-3"))
    lead.client.post(_path(hook), content=bad, headers=_github(bad, delivery="d-4"))
    assert recorder.requests == []
    lead.client.post(_path(hook), content=ping, headers=_github(ping, event="ping", delivery="d-5"))
    assert len(recorder.requests) == 1


def test_concurrent_approvals_of_one_delivery_queue_and_forward_once(sqlite_engine, lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _create(admin, world, forward_url="https://gw.test/hook")
    _set_secret(admin, hook)
    _rule(lead, hook, world.wf)
    raw = _repo()
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [held] = _held(sqlite_engine, hook["id"])
    monkeypatch.setattr(hooks, "held_delivery", lambda conn, hook, delivery_id: held)
    url = f"/api/webhooks/{hook['id']}/deliveries/{held.id}/approve"
    assert lead.client.post(url, json={}, headers=lead.headers).status_code == 200
    late = lead.client.post(url, json={}, headers=lead.headers)
    assert late.status_code == 409 and "held" in late.json()["error"]
    assert len(_jobs(sqlite_engine)) == 1 and len(recorder.requests) == 1


def test_approve_needs_a_sender_unless_generic(sqlite_engine, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    raw = _body({"n": 1})
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [held] = _held(sqlite_engine, hook["id"])
    refused = lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held.id}/approve", json={},
                               headers=lead.headers)
    assert refused.status_code == 409 and refused.json()["error"] == "This delivery has no sender to pin."
    generic = _create(lead, world, "generic", generic_auth="bearer")
    _set_secret(lead, generic)
    lead.client.post(_path(generic), content=raw, headers={"Authorization": f"Bearer {SECRET}"})
    [held] = _held(sqlite_engine, generic["id"])
    approved = lead.client.post(f"/api/webhooks/{generic['id']}/deliveries/{held.id}/approve", json={},
                                headers=lead.headers).json()
    assert approved["webhook"]["approval"] == "approved" and approved["webhook"]["pinned_senders"] is None


def test_approve_and_replay_refuse_a_disabled_webhook(sqlite_engine, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    for n in range(2):
        raw = _repo(1, n=n)
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}"))
    first, second = _held(sqlite_engine, hook["id"])
    base = f"/api/webhooks/{hook['id']}/deliveries"
    lead.client.post(f"{base}/{first.id}/approve", json={}, headers=lead.headers)
    lead.client.post(f"/api/webhooks/{hook['id']}", json={"enabled": False}, headers=lead.headers)
    for action in ("approve", "replay"):
        refused = lead.client.post(f"{base}/{second.id}/{action}", json={}, headers=lead.headers)
        assert refused.status_code == 409 and "Enable" in refused.json()["error"]
    assert [d.id for d in _held(sqlite_engine, hook["id"])] == [second.id]


def test_reject_is_only_for_pending_and_dismiss_only_for_approved(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _approve(lead, hook, pinned_senders=["github:repo:1"])
    raw = _repo(2)
    lead.client.post(_path(hook), content=raw, headers=_github(raw))
    [held] = _held(sqlite_engine, hook["id"])
    refused = lead.client.post(f"/api/webhooks/{hook['id']}/reject", json={}, headers=lead.headers)
    assert refused.status_code == 409 and _hook_row(sqlite_engine, hook["id"]) is not None
    base = f"/api/webhooks/{hook['id']}/deliveries/{held.id}"
    assert lead.client.post(f"{base}/dismiss", json={}, headers=lead.headers).json() == {"ok": True}
    [row] = _deliveries(sqlite_engine, hook["id"])
    assert (row.status, row.reason) == ("ignored", "dismissed") and _jobs(sqlite_engine) == []
    assert lead.client.post(f"{base}/dismiss", json={}, headers=lead.headers).status_code == 409
    pending = _create(lead, world, "slack", secret_name="SLACK_ONE")
    _set_secret(lead, pending)
    slack_raw = _body({"type": "event_callback", "team_id": "T1", "event_id": "E1", "event": {"type": "message"}})
    lead.client.post(_path(pending), content=slack_raw, headers=_slack(slack_raw, lead.client.app.state.server.now))
    [slack_held] = _held(sqlite_engine, pending["id"])
    dismissed = lead.client.post(f"/api/webhooks/{pending['id']}/deliveries/{slack_held.id}/dismiss", json={},
                                 headers=lead.headers)
    assert dismissed.status_code == 409
    with sqlite_engine.connect() as conn:
        [entry] = conn.execute(select(audit.c.detail).where(audit.c.action == "webhook.dismiss")).all()
    assert entry.detail["sender"] == "github:repo:2"


def test_reject_keeps_a_secret_a_rule_hands_to_runs(sqlite_engine, lead, world):
    other = _hook(lead, world, "slack")
    _rule(lead, other, world.wf, secrets=["GH_SECRET"])
    hook = _create(lead, world, secret_name="GH_SECRET")
    _set_secret(lead, hook)
    lead.client.post(f"/api/webhooks/{hook['id']}/reject", json={}, headers=lead.headers)
    with sqlite_engine.connect() as conn:
        assert _hook_row(sqlite_engine, hook["id"]) is None
        assert "GH_SECRET" in [s["name"] for s in secret_store.list_secrets(conn, world.team)]


@pytest.mark.parametrize("source,cfg", [
    ("github", {}), ("jira", {}), ("slack", {}), ("generic", {"generic_auth": "hmac"}),
    ("generic", {"generic_auth": "hmac", "generic_signature_header": "X-Ci-Sig"}),
    ("generic", {"generic_auth": "bearer"}),
])
def test_signed_headers_pass_the_sources_own_check(source, cfg, clock):
    raw = b'{"a":1}'
    headers = hooks.signed_headers(source, cfg, SECRET, {"content-type": "application/json"}, raw, clock())
    assert hooks.verify(source, cfg, SECRET, dict(headers), raw, clock()) is None
    assert ("content-type", "application/json") in headers


def test_signed_headers_leave_linear_unsigned_and_drop_stale_signatures(clock):
    stored = {"content-type": "application/json", "x-hub-signature-256": "sha256=old"}
    assert hooks.signed_headers("linear", {}, SECRET, stored, b"{}", clock()) == [("content-type", "application/json")]
    assert hooks.signed_headers("github", {}, None, stored, b"{}", clock()) == [("content-type", "application/json")]


def test_replayed_slack_forward_is_signed_with_a_fresh_timestamp(sqlite_engine, keyring, lead, admin, world, clock,
                                                                monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _create(admin, world, "slack", forward_url="https://gw.test/slack")
    _set_secret(admin, hook)
    raw = _body({"type": "event_callback", "team_id": "T1", "event_id": "E1", "event": {"type": "message"}})
    lead.client.post(_path(hook), content=raw, headers=_slack(raw, clock))
    [held] = _held(sqlite_engine, hook["id"])
    clock.advance(seconds=600)
    headers = _as(lead.client, sqlite_engine, keyring, clock, world.users["lead"])
    lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held.id}/approve", json={}, headers=headers)
    [sent] = recorder.requests
    assert hooks.verify("slack", {}, SECRET, dict(sent.headers), sent.content, clock()) is None
    with sqlite_engine.connect() as conn:
        stored = conn.execute(select(webhook_deliveries.c.headers)).scalar()
    assert SECRET not in json.dumps(stored) and "x-slack-signature" not in stored


# sender pinning

def test_an_unpinned_approved_webhook_forwards_every_signed_sender(sqlite_engine, lead, admin, world, monkeypatch):
    recorder = _Recorder()
    _patch_httpx(monkeypatch, recorder)
    hook = _hook(admin, world, forward_url="https://gw.test/hook")
    for n in (1, 2):
        raw = _repo(n)
        assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d{n}")).json()["status"] \
            == "ignored"
    assert len(recorder.requests) == 2 and _held(sqlite_engine, hook["id"]) == []


def _post_from(lead, hook, *repos):
    for n in repos:
        raw = _repo(n)
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"r{n}"))


def test_turning_pinning_on_takes_the_previewed_senders(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _post_from(lead, hook, 2, 1, 2)
    base = f"/api/webhooks/{hook['id']}/pinning"
    preview = lead.client.get(base).json()["senders"]
    assert preview == ["github:repo:1", "github:repo:2"]
    stale = lead.client.post(base, json={"senders": ["github:repo:1"]}, headers=lead.headers)
    assert stale.status_code == 409 and "changed" in stale.json()["error"]
    on = lead.client.post(base, json={"senders": preview}, headers=lead.headers).json()["webhook"]
    assert on["pinned_senders"] == preview
    assert lead.client.post(base, json={"senders": preview}, headers=lead.headers).status_code == 409
    _post_from(lead, hook, 3)
    assert [d.sender for d in _held(sqlite_engine, hook["id"])] == ["github:repo:3"]


def test_preview_leaves_out_held_and_dismissed_senders(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _post_from(lead, hook, 1)
    lead.client.post(f"/api/webhooks/{hook['id']}/pinning", json={"senders": ["github:repo:1"]}, headers=lead.headers)
    _post_from(lead, hook, 2, 3)
    held = {d.sender: d for d in _held(sqlite_engine, hook["id"])}
    lead.client.post(f"/api/webhooks/{hook['id']}/deliveries/{held['github:repo:2'].id}/dismiss", json={},
                     headers=lead.headers)
    lead.client.post(f"/api/webhooks/{hook['id']}/pinning/off", json={}, headers=lead.headers)
    assert lead.client.get(f"/api/webhooks/{hook['id']}/pinning").json()["senders"] == ["github:repo:1"]


def test_turning_pinning_on_needs_a_sender_and_a_pinnable_webhook(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    empty = lead.client.post(f"/api/webhooks/{hook['id']}/pinning", json={"senders": []}, headers=lead.headers)
    assert empty.status_code == 409 and "No delivery" in empty.json()["error"]
    generic = _hook(lead, world, "generic", generic_auth="bearer")
    refused = lead.client.post(f"/api/webhooks/{generic['id']}/pinning", json={"senders": []}, headers=lead.headers)
    assert refused.status_code == 409 and "Generic" in refused.json()["error"]
    pending = _create(lead, world, "slack")
    refused = lead.client.post(f"/api/webhooks/{pending['id']}/pinning/off", json={}, headers=lead.headers)
    assert refused.status_code == 409 and "Approve" in refused.json()["error"]


def test_remove_one_pinned_sender_but_never_the_last(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _approve(lead, hook, pinned_senders=["github:repo:1", "github:repo:2"])
    base = f"/api/webhooks/{hook['id']}/pinning"
    removed = lead.client.post(f"{base}/remove", json={"sender": "github:repo:1"}, headers=lead.headers).json()
    assert removed["webhook"]["pinned_senders"] == ["github:repo:2"]
    assert lead.client.post(f"{base}/remove", json={"sender": "github:repo:9"}, headers=lead.headers).status_code \
        == 404
    last = lead.client.post(f"{base}/remove", json={"sender": "github:repo:2"}, headers=lead.headers)
    assert last.status_code == 409 and "Turn sender pinning off" in last.json()["error"]
    off = lead.client.post(f"{base}/off", json={}, headers=lead.headers).json()["webhook"]
    assert off["pinned_senders"] is None
    assert lead.client.post(f"{base}/off", json={}, headers=lead.headers).status_code == 409
    with sqlite_engine.connect() as conn:
        actions = [r.action for r in conn.execute(select(audit.c.action).where(audit.c.action.in_(
            ["webhook.unpin", "webhook.pin_off", "webhook.pin_on"])))]
    assert actions == ["webhook.unpin", "webhook.pin_off"]


def test_approving_on_an_unpinned_webhook_replays_without_pinning(sqlite_engine, lead, world):
    hook = _create(lead, world)
    _set_secret(lead, hook)
    _rule(lead, hook, world.wf)
    _post_from(lead, hook, 1, 2)
    held = {d.sender: d for d in _held(sqlite_engine, hook["id"])}
    base = f"/api/webhooks/{hook['id']}/deliveries"
    lead.client.post(f"{base}/{held['github:repo:1'].id}/approve", json={}, headers=lead.headers)
    lead.client.post(f"/api/webhooks/{hook['id']}/pinning/off", json={}, headers=lead.headers)
    approved = lead.client.post(f"{base}/{held['github:repo:2'].id}/approve", json={}, headers=lead.headers).json()
    assert approved["jobs"] == 1 and approved["webhook"]["pinned_senders"] is None


def test_pinning_routes_need_a_lead_with_step_up(app, sqlite_engine, keyring, clock, world, lead):
    hook = _hook(lead, world)
    base = f"/api/webhooks/{hook['id']}/pinning"
    posts = [base, f"{base}/off", f"{base}/remove"]
    no_step_up = _as(app, sqlite_engine, keyring, clock, world.users["lead"], step_up=False)
    assert [app.post(p, json={}, headers=no_step_up).status_code for p in posts] == [403] * 3
    assert app.get(base).status_code == 200
    for role in ("contributor", "viewer"):
        headers = _as(app, sqlite_engine, keyring, clock, world.users[role])
        assert [app.post(p, json={}, headers=headers).status_code for p in posts] == [403] * 3
        assert app.get(base).status_code == 403
    _as(app, sqlite_engine, keyring, clock, world.users["other_lead"])
    assert app.get(base).status_code == 404


def test_pin_changes_read_the_current_row_not_a_stale_one(sqlite_engine, lead, world):
    hook = _hook(lead, world)
    _approve(lead, hook, pinned_senders=["github:repo:1", "github:repo:2"])
    stale = _hook_row(sqlite_engine, hook["id"])
    _approve(lead, hook, pinned_senders=["github:repo:1", "github:repo:2", "github:repo:3"])
    with sqlite_engine.begin() as conn:
        hooks.unpin(conn, stale, "github:repo:1", actor_id=None, now=stale.updated_at, ip=None)
    assert _hook_row(sqlite_engine, hook["id"]).pinned_senders == ["github:repo:2", "github:repo:3"]


def test_webhooks_migrated_from_0011_still_admit_and_forward(tmp_path, keyring, clock, monkeypatch):
    from sqlalchemy import text

    from armature.server.db import make_engine, migrate
    url = f"sqlite:///{tmp_path / 'prod-shape.db'}"
    migrate(url, "0011")
    team = new_id()
    shapes = [("gh", "github", "tok-gh", None, "http://127.0.0.1:8099/gh/pr"),
              ("sl1", "slack", "tok-s1", None, "http://127.0.0.1:8099/slack/1"),
              ("sl2", "slack", "tok-s2", None, "http://127.0.0.1:8099/slack/2"),
              ("ci", "generic", "tok-ci", "bearer", "http://127.0.0.1:8099/ci/build")]
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            at = {"at": "2026-10-27 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'own', :at)"), {"t": team, **at})
            for name, source, token, auth, forward in shapes:
                conn.execute(text(
                    "INSERT INTO webhooks (id, team_id, name, source, path_token, secret_name, generic_auth, "
                    "forward_url, enabled, created_at, updated_at) VALUES (:i, :t, :n, :s, :k, :n, :a, :f, :on, :at, :at)"),
                    {"i": new_id(), "t": team, "n": name, "s": source, "k": token, "a": auth, "f": forward,
                     "on": True, **at})
        migrate(url)
        with engine.begin() as conn:
            for name, *_ in shapes:
                secret_store.set_secret(conn, keyring, team_id=team, name=name, value=SECRET, actor_id=None)
    finally:
        engine.dispose()
    engine = make_engine(url)
    try:
        recorder = _Recorder()
        _patch_httpx(monkeypatch, recorder)
        client = make_client(engine, keyring, clock)
        gh = _repo()
        slack = _body({"type": "event_callback", "event_id": "E1", "team_id": "T1", "event": {"type": "message"}})
        ci = _body({"kind": "build"})
        sends = [("tok-gh", gh, _github(gh)), ("tok-s1", slack, _slack(slack, clock)),
                 ("tok-s2", slack, _slack(slack, clock)),
                 ("tok-ci", ci, {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"})]
        for token, raw, headers in sends:
            response = client.post(f"/hooks/{token}", content=raw, headers=headers)
            assert response.status_code == 200 and response.json()["status"] != "held", token
        assert [str(r.url) for r in recorder.requests] == [f[4] for f in shapes]
        with engine.connect() as conn:
            assert {r.approval for r in conn.execute(select(webhooks)).all()} == {"approved"}
            assert [d.status for d in conn.execute(select(webhook_deliveries)).all()] == ["ignored"] * 4
    finally:
        engine.dispose()


def test_a_rule_without_a_key_queues_exactly_as_before(sqlite_engine, lead, world):
    assert hooks.check_rule({}) == {"order": 0, "stop": False, "shadow": False, "dry": False, "events": [],
                                    "conditions": [], "inputs": {}, "secrets": []}
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf, events=["pull_request"], inputs={"pr": "$.number"})
    assert "debounce_seconds" not in rule["config"] and "concurrency" not in rule["config"]
    for n in range(2):
        raw = _body({"number": 7, "n": n})
        lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"d-{n}"))
    rows = _jobs(sqlite_engine)
    assert [r.status for r in rows] == ["queued", "queued"]
    assert {(r.concurrency_key, r.not_before, r.superseded_by) for r in rows} == {(None, None, None)}


def test_a_synchronize_storm_leaves_one_job_after_ten_quiet_minutes(sqlite_engine, lead, world, clock):
    hook = _hook(lead, world)
    rule = _rule(lead, hook, world.wf, events=["pull_request"], key=["repository.full_name", "number"],
                 concurrency="supersede", sticky=["full"], inputs={"pr": "$.number", "full": "$.full"})
    assert rule["config"]["debounce_seconds"] == hooks.PR_QUIET_S
    for n in range(10):
        raw = _body({"action": "synchronize", "number": 7, "repository": {"full_name": "Acme/App"},
                     **({"full": True} if n == 3 else {})})
        assert lead.client.post(_path(hook), content=raw, headers=_github(raw, delivery=f"s-{n}")).json() == {
            "status": "accepted", "jobs": 1}
        clock.advance(seconds=1)
    other = _body({"action": "synchronize", "number": 8, "repository": {"full_name": "Acme/App"}})
    lead.client.post(_path(hook), content=other, headers=_github(other, delivery="other"))
    rows = _jobs(sqlite_engine)
    queued = [r for r in rows if r.status == "queued"]
    assert [r.status for r in rows].count("superseded") == 9
    assert [r.concurrency_key for r in queued] == ["Acme/App#7", "Acme/App#8"]
    newest = queued[0]
    assert newest.not_before.replace(tzinfo=None) == (clock() - timedelta(seconds=1) + timedelta(minutes=10)) \
        .replace(tzinfo=None)
    assert newest.inputs == {"pr": 7, "full": True}
    storm = [r for r in rows if r.concurrency_key == "Acme/App#7"]
    assert [r.superseded_by for r in storm[:-1]] == [r.id for r in storm[1:]] and storm[-1].id == newest.id
    listed = lead.client.get("/api/jobs").json()["jobs"]
    assert {j["id"] for j in listed} == {r.id for r in queued}
    assert {j["concurrency_key"] for j in listed} == {"Acme/App#7", "Acme/App#8"} and listed[0]["not_before"]
    superseded = lead.client.get("/api/jobs?status=superseded").json()["jobs"]
    assert len(superseded) == 9 and superseded[0]["superseded_by"] == newest.id


@pytest.mark.parametrize("config,message", [
    ({"concurrency": "supersede"}, "needs a key"),
    ({"key": "number", "concurrency": "queue"}, "list of 1 to 3"),
    ({"key": ["a", "b", "c", "d"]}, "list of 1 to 3"),
    ({"key": ["a b"]}, "dotted path"),
    ({"key": ["number"], "concurrency": "fifo"}, "concurrency must be"),
    ({"key": ["number"], "debounce_seconds": 3601}, "debounce_seconds"),
    ({"key": ["number"], "concurrency": "queue", "cancel_running": True}, "need concurrency supersede"),
    ({"key": ["number"], "concurrency": "supersede", "sticky": ["bad-name"]}, "input names"),
])
def test_bad_concurrency_settings_are_refused(config, message):
    with pytest.raises(hooks.WebhookError, match=message):
        hooks.check_rule(config)


def test_render_key_needs_every_path():
    assert hooks.render_key(["repo", "n"], {"repo": "a/b", "n": 3}) == "a/b#3"
    assert hooks.render_key(["repo", "n"], {"repo": "a/b"}) is None
    assert hooks.render_key(["flag"], {"flag": True}) is None


def test_the_pull_request_quiet_period_defaults_only_for_supersede():
    events = {"events": ["pull_request"], "key": ["number"]}
    assert hooks.check_rule({**events, "concurrency": "supersede"})["debounce_seconds"] == hooks.PR_QUIET_S
    assert "debounce_seconds" not in hooks.check_rule({**events, "concurrency": "queue"})
    assert "debounce_seconds" not in hooks.check_rule(events)
