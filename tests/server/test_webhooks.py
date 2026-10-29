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


def _hook(lead, world, source="github", **body):
    hook = _create(lead, world, source, **body)
    _set_secret(lead, hook)
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
