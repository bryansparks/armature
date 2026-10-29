"""The authoring assistant: who may ask, the limits, the conversation a request starts, and that nothing is saved."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

import pytest
from sqlalchemy import func, insert, select, update

from armature.packs import AdapterDef, Pack
from armature.server import assist, defs, llm
from armature.server import conversations as conversation_module
from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    conversations,
    jobs,
    projects,
    providers,
    run_steps,
    runs,
    teams,
    versions,
    workflows,
)
from tests.server.app_support import Clock, add_membership, add_user, make_client, mutation, sign_in_as

GOOD = """name: review
description: Review one pull request.
stages:
  - id: resolve
    adapter: demo/resolve
    with:
      number: 3
"""

RENAMED = GOOD.replace("Review one pull request.", "Review a pull request.")
WITH_RAW = GOOD + """adapters:
  build:
    name: build
    type: script
    cmd: ./build.sh
"""

ROLE_DOC = """---
name: security
tools: [Read]
---

You are the security reviewer.
"""


class FakeCatalog:
    adapters: ClassVar[dict[str, AdapterDef]] = {
        "demo/resolve": AdapterDef(description="Read a pull request.", cmd="resolve.sh", params={
            "type": "object", "properties": {"number": {"type": "integer"}}, "additionalProperties": False}),
    }

    def get(self, ref):
        return self.adapters.get(ref)

    def packs(self):
        return [Pack(name="demo", version="1.0.0", adapters={"resolve": self.adapters["demo/resolve"]})]

    def tools_allowlist(self):
        return ["Read", "Grep"]

    def mcp_allowlist(self):
        return ["github"]


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(defs, "load_catalog", lambda packs_dir=None: FakeCatalog())


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    def refuse(**_):
        raise AssertionError("the server called a model")
    monkeypatch.setattr(llm, "litellm_completion", refuse)


@dataclass
class World:
    engine: object
    keyring: object
    clock: Clock
    team: str
    other: str
    project: str
    team_wf: str
    project_wf: str
    instance_wf: str
    role_doc: str
    users: dict


def _add(conn, kind, scope, name, body, clock):
    row_id = new_id()
    values = {"id": row_id, kind.name_col: name, "current_version": 1, "created_at": clock(),
              "updated_at": clock(), **scope.columns()}
    if kind is defs.DOCUMENT:
        values["kind"] = "role"
    conn.execute(insert(kind.table).values(**values))
    defs._add_version(conn, kind, row_id, 1, body, "seed", None, clock())
    return row_id


@pytest.fixture
def world(engine, keyring):
    clock = Clock()
    team, other, project = new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "own", "created_at": clock()},
                                           {"id": other, "name": "other", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=clock()))
        for team_id in (team, other):
            conn.execute(insert(providers).values(id=new_id(), team_id=team_id, kind="anthropic",
                                                  config={"credential_secret": "ANTHROPIC_KEY"},
                                                  aliases={"lead": "claude-opus", "fast": "claude-haiku"},
                                                  created_at=clock(), updated_at=clock()))
            secret_store.set_secret(conn, keyring, team_id=team_id, name="ANTHROPIC_KEY", value=f"key-{team_id}",
                                    actor_id=None)
        team_wf = _add(conn, defs.WORKFLOW, defs.DefScope("team", team), "review", GOOD, clock)
        project_wf = _add(conn, defs.WORKFLOW, defs.DefScope("project", team, project), "review", GOOD, clock)
        instance_wf = _add(conn, defs.WORKFLOW, defs.INSTANCE, "review", GOOD, clock)
        role_doc = _add(conn, defs.DOCUMENT, defs.DefScope("team", team), "security", ROLE_DOC, clock)
    users = {name: add_user(engine, keyring, clock, name, is_admin=name == "admin")[0]
             for name in ("admin", "lead", "plead", "contrib", "viewer", "outsider")}
    add_membership(engine, clock, users["lead"], "team", team, "team_lead")
    add_membership(engine, clock, users["plead"], "project", project, "project_lead")
    add_membership(engine, clock, users["contrib"], "project", project, "contributor")
    add_membership(engine, clock, users["viewer"], "team", team, "viewer")
    add_membership(engine, clock, users["outsider"], "team", other, "team_lead")
    return World(engine, keyring, clock, team, other, project, team_wf, project_wf, instance_wf, role_doc, users)


def _client(world, who):
    client = make_client(world.engine, world.keyring, world.clock)
    client.headers.update(mutation(sign_in_as(client, world.engine, world.keyring, world.clock, world.users[who])))
    return client


def _ask(client, **body):
    payload = {"kind": "workflow", "body": GOOD, "instruction": "Reword the description.", **body}
    return client.post("/api/assist", json=payload)


@pytest.mark.parametrize(("who", "target", "expected"), [
    ("admin", "team_wf", 200), ("lead", "team_wf", 200), ("plead", "team_wf", 403), ("contrib", "team_wf", 403),
    ("viewer", "team_wf", 403), ("outsider", "team_wf", 404),
    ("plead", "project_wf", 200), ("contrib", "project_wf", 403), ("lead", "project_wf", 200),
])
def test_only_an_editor_of_the_object_may_ask(world, who, target, expected):
    response = _ask(_client(world, who), id=getattr(world, target))
    assert response.status_code == expected, response.text
    assert len(_turns(world)) == (1 if expected == 200 else 0)


def _turns(world):
    with world.engine.connect() as conn:
        return conn.execute(select(jobs).order_by(jobs.c.created_at, jobs.c.id)).all()


def _free(world):
    """End every running turn, as the sweep does once a turn's job finishes."""
    with world.engine.begin() as conn:
        conn.execute(update(conversations).values(active_job_id=None))


def test_a_new_object_needs_a_creator_in_the_picked_team(world):
    assert _ask(_client(world, "plead"), team_id=world.team).status_code == 200
    assert _ask(_client(world, "contrib"), team_id=world.team).status_code == 403
    assert _ask(_client(world, "lead"), team_id=world.other).status_code == 404
    assert _ask(_client(world, "lead")).status_code == 400


def test_an_instance_object_uses_the_team_the_user_picks(world):
    admin = _client(world, "admin")
    assert _ask(admin, id=world.instance_wf).status_code == 400
    response = _ask(admin, id=world.instance_wf, team_id=world.other)
    assert response.status_code == 200, response.text
    assert response.json()["conversation"]["team_id"] == world.other
    assert _ask(_client(world, "lead"), id=world.instance_wf, team_id=world.team).status_code == 403


def test_a_request_queues_a_turn_of_the_teams_assistant_and_saves_nothing(world):
    response = _ask(_client(world, "lead"), id=world.team_wf, alias="fast")
    assert response.status_code == 200, response.text
    shown = response.json()["conversation"]
    assert shown["kind"] == "web" and shown["working"] is True and response.json()["seq"] == 1
    assert shown["messages"][0]["text"] == "Reword the description."
    [job] = _turns(world)
    with world.engine.connect() as conn:
        assistant = conn.execute(select(workflows).where(workflows.c.id == job.workflow_id)).one()
        assert (assistant.name, assistant.team_id, assistant.built_in) == (conversation_module.ASSISTANT, None, True)
        assert conn.execute(select(workflows.c.current_version).where(workflows.c.id == world.team_wf)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(versions)
                            .where(versions.c.object_id == world.team_wf)).scalar() == 1
        row = conn.execute(select(audit).where(audit.c.action == "assist.request")).one()
    assert job.inputs == {"conversation_id": shown["id"], "message_seq": 1}
    assert row.team_id == world.team and row.detail["conversation_id"] == shown["id"]
    _free(world)
    assert _ask(_client(world, "lead"), id=world.team_wf).status_code == 200
    assert len({r.workflow_id for r in _turns(world)}) == 1


def test_a_follow_up_joins_the_users_own_conversation(world):
    client = _client(world, "lead")
    first = _ask(client, id=world.team_wf).json()["conversation"]["id"]
    again = _ask(client, id=world.team_wf, conversation_id=first, instruction="And shorten it.")
    assert again.status_code == 200 and again.json()["conversation"]["id"] == first
    assert again.json()["seq"] == 2 and len(_turns(world)) == 1
    assert _ask(_client(world, "admin"), id=world.team_wf, conversation_id=first).status_code == 404


def test_requests_are_limited_to_thirty_an_hour(world):
    client = _client(world, "lead")
    for _ in range(assist.RATE_LIMIT):
        assert _ask(client, id=world.team_wf).status_code == 200
        _free(world)
    assert _ask(client, id=world.team_wf).status_code == 429
    world.clock.advance(minutes=61)
    assert _ask(_client(world, "lead"), id=world.team_wf).status_code == 200


def test_bad_requests_are_refused(world):
    client = _client(world, "lead")
    assert _ask(client, id=world.team_wf, instruction="  ").status_code == 400
    assert _ask(client, id=world.team_wf, alias="Not An Alias").status_code == 400
    assert _ask(client, id=world.team_wf, kind="setting").status_code == 400
    assert _ask(client, id=world.team_wf, body="x" * (defs.BODY_MAX + 1)).status_code == 400
    assert _turns(world) == []


def test_it_needs_a_signed_in_session_and_csrf(world):
    client = _client(world, "lead")
    client.headers.pop("X-CSRF-Token")
    assert _ask(client, id=world.team_wf).status_code == 403


def test_a_team_has_a_daily_cap(world):
    def client(who):
        c = make_client(world.engine, world.keyring, world.clock, assist={"team_daily_limit": 2})
        c.headers.update(mutation(sign_in_as(c, world.engine, world.keyring, world.clock, world.users[who])))
        return c
    assert _ask(client("lead"), id=world.team_wf).status_code == 200
    assert _ask(client("admin"), id=world.team_wf).status_code == 200
    assert _ask(client("plead"), id=world.project_wf).status_code == 429
    assert _ask(client("outsider"), team_id=world.other).status_code == 200


def test_at_most_two_turns_run_at_once_per_user(world):
    client = _client(world, "lead")
    assert _ask(client, id=world.team_wf).status_code == 200
    assert _ask(client, id=world.team_wf).status_code == 200
    assert _ask(client, id=world.team_wf).status_code == 429
    assert _ask(_client(world, "admin"), id=world.team_wf).status_code == 200


def test_a_stale_base_version_is_refused(world):
    response = _propose(_client(world, "lead"), workflow_id=world.team_wf, base_version=0)
    assert response.status_code == 409 and "changed since" in response.text
    assert _turns(world) == []


def _draft(world, conversation_id, body):
    with world.engine.begin() as conn:
        conv = conversation_module.get(conn, conversation_id)
        job = conn.execute(select(jobs).where(jobs.c.id == conv.active_job_id)).one()
        conversation_module.decide(conn, conv, job, {"action": "draft", "reply": "Done.", "body": body},
                                   world.clock(), FakeCatalog())
        return conversation_module.public(conn, conversation_module.get(conn, conversation_id))["draft"]


def _context(world, conversation_id):
    with world.engine.connect() as conn:
        conv = conversation_module.get(conn, conversation_id)
        job = conn.execute(select(jobs).where(jobs.c.id == conv.active_job_id)).one()
        return conversation_module.history(conn, conv, job, FakeCatalog())["context"]


def test_a_draft_carries_the_summary_and_changes_against_the_saved_version(world):
    added = GOOD.replace("number: 3", "number: 4") + "  - id: approve\n    gate: human\n    depends_on: [resolve]\n"
    shown = _propose(_client(world, "admin"), workflow_id=world.team_wf, base_version=1).json()["conversation"]
    draft = _draft(world, shown["id"], added)
    assert draft["validation"]["ok"] is True and draft["base_version"] == 1 and draft["team_name"] == "own"
    assert [step["id"] for step in draft["summary"]["steps"]] == ["resolve", "approve"]
    assert draft["changes"]["added"] == ["approve"] and draft["changes"]["changed"] == [
        {"id": "resolve", "fields": ["details"]}]
    assert "+    gate: human" in draft["diff"] and draft["diff"].startswith("--- current")


def test_a_non_admin_draft_is_held_to_their_rules(world):
    shown = _ask(_client(world, "lead"), id=world.team_wf).json()["conversation"]
    context = _context(world, shown["id"])
    assert "This user is not an admin" in context and "demo/resolve" in context
    tag = re.search(r"<<<BODY ([0-9a-f]{16})\n", context).group(1)
    assert f"<<<BODY {tag}\n{GOOD}\nBODY {tag}>>>" in context
    draft = _draft(world, shown["id"], WITH_RAW)
    assert draft["validation"]["ok"] is False


def test_a_document_draft_has_no_workflow_summary(world):
    shown = _ask(_client(world, "lead"), kind="document", id=world.role_doc, body=ROLE_DOC).json()["conversation"]
    assert "Read" in _context(world, shown["id"])
    draft = _draft(world, shown["id"], ROLE_DOC)
    assert "summary" not in draft and "changes" not in draft


def test_unsaved_editor_text_is_where_the_draft_starts(world):
    shown = _ask(_client(world, "lead"), id=world.team_wf, body=RENAMED).json()["conversation"]
    assert "Review a pull request." in _context(world, shown["id"])


def test_an_instance_workflow_uses_the_oldest_membership_with_the_alias(world):
    admin = world.users["admin"]
    add_membership(world.engine, world.clock, admin, "team", world.other, "viewer")
    world.clock.advance(seconds=1)
    add_membership(world.engine, world.clock, admin, "team", world.team, "viewer")
    assert _propose(_client(world, "admin"), workflow_id=world.instance_wf).json()["conversation"]["team_id"] \
        == world.other
    with world.engine.begin() as conn:
        conn.execute(providers.update().where(providers.c.team_id == world.other).values(aliases={"fast": "m"}))
    assert _propose(_client(world, "admin"), workflow_id=world.instance_wf).json()["conversation"]["team_id"] \
        == world.team


def test_a_new_workflow_is_drafted_from_its_name_then_created_by_a_person(world):
    client = _client(world, "lead")
    shown = _propose(client, scope_kind="project", scope_id=world.project, name="review-two",
                     instruction="Read pull request 3.").json()["conversation"]
    context = _context(world, shown["id"])
    assert "review-two" in context
    draft = _draft(world, shown["id"], GOOD)
    assert draft["changes"]["added"] == ["resolve"] and draft["base_version"] is None
    created = client.post("/api/workflows", json={"name": "review-two", "scope_kind": "project",
                                                  "scope_id": world.project, "body": draft["body"],
                                                  "note": "Read pull request 3."})
    assert created.status_code == 201, created.text


def test_a_new_workflow_takes_its_team_from_the_scope(world):
    plead = _client(world, "plead")
    response = _propose(plead, scope_kind="project", scope_id=world.project, name="x")
    assert response.status_code == 200 and response.json()["conversation"]["team_id"] == world.team
    assert _propose(_client(world, "lead"), scope_kind="team", scope_id=world.team, name="x").status_code == 200
    assert _propose(_client(world, "lead"), scope_kind="team", scope_id=world.other, name="x").status_code == 404
    assert _propose(plead, scope_kind="project", scope_id="nope", name="x").status_code == 404


def test_a_new_instance_workflow_needs_an_admin(world):
    assert _propose(_client(world, "lead"), scope_kind="instance", name="x").status_code == 403
    add_membership(world.engine, world.clock, world.users["admin"], "team", world.team, "viewer")
    response = _propose(_client(world, "admin"), scope_kind="instance", name="x")
    assert response.status_code == 200 and response.json()["conversation"]["team_id"] == world.team


def test_applying_after_someone_else_saved_is_a_conflict(world):
    client = _client(world, "lead")
    shown = _propose(client, workflow_id=world.team_wf, base_version=1).json()["conversation"]
    draft = _draft(world, shown["id"], RENAMED)
    other = _client(world, "admin")
    assert other.post(f"/api/workflows/{world.team_wf}", json={"body": GOOD + "# x\n", "base_version": 1}
                      ).status_code == 200
    applied = client.post(f"/api/workflows/{world.team_wf}", json={"body": draft["body"], "note": "Reword",
                                                                    "base_version": draft["base_version"]})
    assert applied.status_code == 409
    assert applied.json()["current_version"] == 2


def _propose(client, **body):
    return client.post("/api/assist", json={"instruction": "Reword the description.", **body})



def test_the_detail_tells_the_workflow_and_its_webhook_rules(world):
    client = _client(world, "lead")
    hook = client.post(f"/api/teams/{world.team}/webhooks", json={"name": "gh", "source": "github"}).json()["webhook"]
    rule = client.post(f"/api/webhooks/{hook['id']}/rules",
                       json={"workflow_id": world.team_wf, "config": {"events": ["pull_request"], "shadow": True}})
    assert rule.status_code in (200, 201), rule.text
    detail = client.get(f"/api/workflows/{world.team_wf}").json()
    assert detail["summary"]["steps"][0]["what"] == "Runs script demo/resolve"
    assert detail["started_by"] == [{"webhook_id": hook["id"], "webhook": "gh", "events": ["pull_request"],
                                     "shadow": True, "enabled": False}]
    shared = client.post(f"/api/webhooks/{hook['id']}/rules", json={"workflow_id": world.instance_wf, "config": {}})
    assert shared.status_code in (200, 201), shared.text
    assert len(client.get(f"/api/workflows/{world.instance_wf}").json()["started_by"]) == 1
    assert _client(world, "outsider").get(f"/api/workflows/{world.instance_wf}").json()["started_by"] == []
    assert "summary" not in client.get(f"/api/documents/{world.role_doc}").json()


TEMPLATED = """name: review
description: "Review {{ number }}."
mission: "Focus on {{ repo }}."
contracts:
  inputs: [{name: number}, {name: repo}]
  outputs: [{name: summary, from: "{{ judge.content }}"}]
stages:
  - id: resolve
    adapter: demo/resolve
    with:
      number: "{{ number }}"
  - id: judge
    role: {name: Judge, type: judge, description: "Judge {{ resolve.files }} in {{ repo }}."}
    depends_on: [resolve]
    skip_if: "{{ resolve.files | length == 0 }}"
    condition: "{{ number }}"
    loop: {max_iterations: 2, until: "{{ judge.content }}"}
  - id: approve
    gate: human
    present: "Approve {{ judge.content }}?"
    depends_on: [judge]
"""


def test_the_server_never_renders_a_spec_template_in_process(world, monkeypatch):
    import jinja2

    def refuse(*args, **kwargs):
        raise AssertionError("the server rendered a spec template in-process")

    monkeypatch.setattr(jinja2.Environment, "compile", refuse)
    monkeypatch.setattr(jinja2.Environment, "from_string", refuse)
    run_id, elsewhere = new_id(), new_id()
    with world.engine.begin() as conn:
        conn.execute(insert(projects).values(id=elsewhere, team_id=world.other, name="web", created_at=world.clock()))
        conn.execute(insert(runs).values(id=run_id, team_id=world.team, workflow_id=world.team_wf,
                                         workflow_name="review", status="succeeded", started_at=world.clock()))
        conn.execute(insert(run_steps).values(run_id=run_id, step_key="judge", step_id="judge", status="succeeded",
                                              output={"content": "{{ 7 * 7 }}"}))
    admin = _client(world, "admin")
    assert admin.post("/api/workflows/validate", json={"body": TEMPLATED}).status_code == 200
    assert admin.post("/api/workflows/parse", json={"body": TEMPLATED}).status_code == 200
    saved = admin.post(f"/api/workflows/{world.team_wf}", json={"body": TEMPLATED, "base_version": 1})
    assert saved.status_code == 200, saved.text
    forked = admin.post(f"/api/workflows/{world.team_wf}/fork", json={"scope_kind": "project", "scope_id": elsewhere})
    assert forked.status_code == 201, forked.text
    assert _ask(admin, id=world.team_wf).status_code == 200
    assert admin.get("/api/runs").status_code == 200
    assert admin.get(f"/api/runs/{run_id}").status_code == 200
