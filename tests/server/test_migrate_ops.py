"""armature-ops workflows imported as database rows: idempotency, validation, and dry-run equivalence with ops."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update
from typer.testing import CliRunner

from armature.cli import app
from armature.server import defs, rbac
from armature.server.config import load_config
from armature.server.db import make_engine, new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    audit,
    documents,
    projects,
    teams,
    triggers,
    versions,
    webhooks,
    workflows,
)
from armature.server.migrate_ops import (
    Document,
    MigrateError,
    build_announce,
    import_ops,
    pr_merged,
    registry,
    resolve_project,
)
from tests.server import ops_harness
from tests.server.tool_support import _connection

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
CHANNEL = "C04BUILDS9Z"
REPOS = ("fs-eng/mobile-ios", "fs-eng/android-tree", "fs-eng/mobile-api-nodejs")
# A workflow that writes to one repository is imported only into its project.
HOME = {"ios-port": "fs-eng/android-tree"}
# How many rules or triggers each import adds. localization, as in ops, runs only by hand.
STARTS = {"localization": 0, "code-review": 2}
SETTINGS = {build_announce.CHANNEL_SETTING: CHANNEL, "slack.intake_channel": "C07MOBINTAKE",
            "jira.site_url": "https://familysearch.atlassian.net", "jira.project": "MEMTEMDIS"}


def _team(conn, slug: str = "familysearch") -> tuple[str, str]:
    """The FamilySearch team with its three projects. Returns the team and the mobile-ios project."""
    team = new_id()
    conn.execute(insert(teams).values(id=team, name="FamilySearch", slug=slug, created_at=NOW))
    for repo in REPOS:
        conn.execute(insert(projects).values(id=new_id(), team_id=team, name=repo.split("/")[1], repo=repo,
                                             created_at=NOW))
    for key, value in SETTINGS.items():
        defs.put_setting(conn, team_id=team, key=key, value=value, actor_id=None, now=NOW, ip=None)
    return team, _project(conn, REPOS[0])


def _project(conn, repo: str) -> str:
    return conn.execute(select(projects.c.id).where(projects.c.repo == repo)).scalar_one()


@pytest.fixture
def world(sqlite_engine, keyring):
    with sqlite_engine.begin() as conn:
        team, project = _team(conn)
    jira_connection(sqlite_engine, keyring, team)
    return team, project


def jira_connection(engine, keyring, team):
    """The team's Jira connection, which development's poll runs through."""
    return _connection(engine, keyring, lambda: NOW, team, "jira", "tracker", {"api_token": "jira-api-token-1"},
                       {"site_url": "https://familysearch.atlassian.net", "email": "bot@familysearch.test"}, {})


def _import(engine, team, project, name, **kwargs):
    with engine.begin() as conn:
        changes, _ = import_ops(conn, name, team_id=team, project_id=project, now=NOW, **kwargs)
    return {c.what + " " + c.name: c.action for c in changes}


def _counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t.name: conn.execute(select(func.count()).select_from(t)).scalar_one()
                for t in (workflows, documents, versions, webhooks, triggers, audit)}


@pytest.mark.parametrize("name", sorted(registry()))
def test_each_body_validates_with_the_builtin_packs(name):
    project = SimpleNamespace(id="p1", repo=HOME.get(name, REPOS[0]), default_branch=None)
    rows = registry()[name](SETTINGS.get, project)
    validation = defs.validate_workflow(rows.body, actor=rbac.Access(is_admin=True), previous_body=None)
    assert validation.ok, validation.to_dict()
    assert "{{" not in str([r.config for r in rows.rules]) and rows.name == name


@pytest.mark.parametrize("name", sorted(registry()))
def test_import_creates_rules_in_shadow_and_dry_and_a_second_import_changes_nothing(sqlite_engine, world, name):
    team, project = world
    if name in HOME:
        with sqlite_engine.connect() as conn:
            project = _project(conn, HOME[name])
    first = _import(sqlite_engine, team, project, name)
    assert first[f"workflow {name}"] == "created"
    assert [a for k, a in first.items() if k.startswith(("rule ", "trigger "))] == ["created"] * STARTS.get(name, 1)
    with sqlite_engine.connect() as conn:
        row = conn.execute(select(workflows).where(workflows.c.name == name)).one()
        rule = conn.execute(select(triggers).where(triggers.c.workflow_id == row.id)).first()
        hooks = conn.execute(select(webhooks)).all()
    if rule is None:
        assert hooks == []
    elif rule.kind == "webhook":
        assert rule.config["shadow"] is True and rule.config["dry"] is True and len(rule.config["import_hash"]) == 64
        assert [h.approval for h in hooks] == ["pending"]
    else:
        assert rule.enabled is False and hooks == []
    assert (row.project_id, row.team_id) == (project, team)
    before = _counts(sqlite_engine)
    second = _import(sqlite_engine, team, project, name)
    assert set(second.values()) == {"unchanged"}
    assert _counts(sqlite_engine) == before


def test_a_reimport_updates_the_body_but_keeps_a_cutover_and_deletes_nothing(sqlite_engine, world, monkeypatch):
    team, project = world
    _import(sqlite_engine, team, project, "pr-merged")
    with sqlite_engine.begin() as conn:
        rule = conn.execute(select(triggers)).one()
        conn.execute(update(triggers).where(triggers.c.id == rule.id)
                     .values(config={**rule.config, "shadow": False, "dry": False}))
        hook = conn.execute(select(webhooks)).one()
        workflow = conn.execute(select(workflows)).one()
        extra = new_id()
        conn.execute(insert(triggers).values(id=extra, team_id=team, workflow_id=workflow.id, kind="webhook",
                                             config={"events": ["push"]}, enabled=True, created_at=NOW,
                                             webhook_id=None))
        hand = defs.create(conn, defs.DOCUMENT, access=rbac.Access(is_admin=True),
                           scope=defs.DefScope("project", team, project), name="notes", body="kept", note=None,
                           actor_id=None, now=NOW, ip=None, doc_kind="prompt")[0]
    monkeypatch.setattr(pr_merged, "BODY", pr_merged.BODY.replace('to: "In Test"', 'to: "QA Review"'))
    changes = _import(sqlite_engine, team, project, "pr-merged")
    assert changes["workflow pr-merged"] == "updated to version 2"
    assert changes[f"rule {hook.name} -> pr-merged"] == "unchanged"
    with sqlite_engine.connect() as conn:
        rules = {r.id: r for r in conn.execute(select(triggers))}
        assert rules[rule.id].config["shadow"] is False and rules[rule.id].config["dry"] is False
        assert extra in rules
        owned = select(documents.c.id).where(documents.c.scope != INSTANCE_SCOPE)
        assert conn.execute(owned).scalars().all() == [hand.id]
        assert "QA Review" in defs.body_of(conn, defs.WORKFLOW, workflow.id, 2)


def test_a_changed_rule_is_updated_in_place(sqlite_engine, world, monkeypatch):
    team, project = world
    _import(sqlite_engine, team, project, "pr-merged")
    with sqlite_engine.connect() as conn:
        changed_from = conn.execute(select(triggers)).one().config["import_hash"]
    config = {**pr_merged.CONFIG, "inputs": {**pr_merged.CONFIG["inputs"], "platform": "ios"}}
    monkeypatch.setattr(pr_merged, "CONFIG", config)
    changes = _import(sqlite_engine, team, project, "pr-merged")
    assert changes["rule github -> pr-merged"] == "updated"
    with sqlite_engine.connect() as conn:
        rule = conn.execute(select(triggers)).one()
    assert rule.config["inputs"]["platform"] == "ios" and rule.config["shadow"] is True
    assert rule.config["import_hash"] != changed_from


def test_an_edited_row_is_refused_until_forced(sqlite_engine, world, monkeypatch):
    team, project = world
    _import(sqlite_engine, team, project, "pr-merged")
    with sqlite_engine.begin() as conn:
        row = conn.execute(select(workflows)).one()
        defs.save(conn, defs.WORKFLOW, row, access=rbac.Access(is_admin=True),
                  body=pr_merged.BODY.replace("projects: [MEMTEMDIS]", "projects: [MEMTEMDIS, MOB]"), note="by hand",
                  base_version=1, actor_id=None, now=NOW, ip=None)
    monkeypatch.setattr(pr_merged, "BODY", pr_merged.BODY.replace('to: "In Test"', 'to: "QA Review"'))
    before = _counts(sqlite_engine)
    with pytest.raises(MigrateError, match="Workflow pr-merged was edited since import, pass --force to overwrite"):
        _import(sqlite_engine, team, project, "pr-merged")
    assert _counts(sqlite_engine) == before
    assert _import(sqlite_engine, team, project, "pr-merged", force=True)["workflow pr-merged"] == \
        "updated to version 3"
    with sqlite_engine.begin() as conn:
        rule = conn.execute(select(triggers)).one()
        conn.execute(update(triggers).where(triggers.c.id == rule.id)
                     .values(config={**rule.config, "conditions": rule.config["conditions"][:1]}))
    monkeypatch.setattr(pr_merged, "CONFIG", {**pr_merged.CONFIG,
                                              "inputs": {**pr_merged.CONFIG["inputs"], "platform": "ios"}})
    with pytest.raises(MigrateError, match="Rule github -> pr-merged was edited since import"):
        _import(sqlite_engine, team, project, "pr-merged")
    assert _import(sqlite_engine, team, project, "pr-merged", force=True)["rule github -> pr-merged"] == "updated"


def test_a_rule_saved_unchanged_without_its_hash_is_restamped(sqlite_engine, world):
    team, project = world
    _import(sqlite_engine, team, project, "pr-merged")
    with sqlite_engine.begin() as conn:
        rule = conn.execute(select(triggers)).one()
        stamp = rule.config["import_hash"]
        conn.execute(update(triggers).where(triggers.c.id == rule.id)
                     .values(config={k: v for k, v in rule.config.items() if k != "import_hash"}))
    assert _import(sqlite_engine, team, project, "pr-merged")["rule github -> pr-merged"] == "updated"
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(triggers)).one().config["import_hash"] == stamp


def test_the_qa_setting_becomes_the_projects_target_status(sqlite_engine, world):
    team, project = world
    with sqlite_engine.begin() as conn:
        android = _project(conn, "fs-eng/android-tree")
        defs.put_setting(conn, team_id=team, project_id=project, key=pr_merged.QA_SETTING, value="In QA",
                         actor_id=None, now=NOW, ip=None)
    _import(sqlite_engine, team, project, "pr-merged")
    _import(sqlite_engine, team, android, "pr-merged")
    with sqlite_engine.connect() as conn:
        bodies = {r.project_id: defs.body_of(conn, defs.WORKFLOW, r.id, 1) for r in conn.execute(select(workflows))}
    assert '      to: "In QA"\n' in bodies[project] and '      to: "In Test"\n' in bodies[android]
    for bad in ("{{ evil }}", "", 7):
        with pytest.raises(MigrateError, match="jira.transition_qa"):
            pr_merged.rows(lambda key, bad=bad: bad if key == pr_merged.QA_SETTING else None)


def test_documents_upsert_on_the_team_or_the_project(sqlite_engine, world, monkeypatch):
    team, project = world
    rows = pr_merged.rows(lambda key: None)
    docs = (Document("role", "simplicity", "---\nname: simplicity\n---\nKeep it small.\n", team=True),
            Document("prompt", "pr_merged", "Comment on the merge."))
    monkeypatch.setattr(pr_merged, "rows", lambda setting, project=None: replace(rows, documents=docs))
    first = _import(sqlite_engine, team, project, "pr-merged")
    assert first["role simplicity"] == "created" and first["prompt pr_merged"] == "created"
    with sqlite_engine.connect() as conn:
        scopes = dict(conn.execute(select(documents.c.key, documents.c.scope)
                                   .where(documents.c.scope != INSTANCE_SCOPE)).all())
    assert scopes == {"simplicity": team, "pr_merged": project}
    assert set(_import(sqlite_engine, team, project, "pr-merged").values()) == {"unchanged"}


def test_an_invalid_body_is_refused_by_the_defs_layer(sqlite_engine, world, monkeypatch):
    team, project = world
    monkeypatch.setattr(pr_merged, "BODY", pr_merged.BODY.replace("jira/transition", "jira/teleport"))
    before = _counts(sqlite_engine)
    with pytest.raises(defs.DefError) as caught:
        _import(sqlite_engine, team, project, "pr-merged")
    assert caught.value.status == 422
    codes = [e["code"] for e in caught.value.extra["validation"]["errors"]]
    assert "UNKNOWN_ADAPTER" in codes
    assert _counts(sqlite_engine) == before


def test_build_announce_needs_its_channel_setting_and_names_no_workflow_it_lacks(sqlite_engine):
    with sqlite_engine.begin() as conn:
        team, project = new_id(), new_id()
        conn.execute(insert(teams).values(id=team, name="Bare", slug="bare", created_at=NOW))
        conn.execute(insert(projects).values(id=project, team_id=team, name="app", created_at=NOW))
        with pytest.raises(MigrateError, match="slack.build_channel"):
            import_ops(conn, "build-announce", team_id=team, project_id=project, now=NOW)
        with pytest.raises(MigrateError, match="No ops workflow nope. Known: build-announce, "):
            import_ops(conn, "nope", team_id=team, project_id=project, now=NOW)


def test_a_webhook_name_held_by_another_source_is_refused(sqlite_engine, world):
    team, project = world
    with sqlite_engine.begin() as conn:
        conn.execute(insert(webhooks).values(id=new_id(), team_id=team, name="github", source="jira",
                                             path_token="t" * 32, secret_name="WEBHOOK_GITHUB", created_at=NOW,
                                             updated_at=NOW))
    with pytest.raises(MigrateError, match="is a jira webhook"):
        _import(sqlite_engine, team, project, "pr-merged")


def test_a_project_resolves_by_team_and_name_or_by_repository(sqlite_engine, world):
    team, project = world
    with sqlite_engine.connect() as conn:
        assert resolve_project(conn, "familysearch/mobile-ios") == (team, project)
        assert resolve_project(conn, "FamilySearch/mobile-ios") == (team, project)
        assert resolve_project(conn, "FS-ENG/Mobile-iOS") == (team, project)
        with pytest.raises(MigrateError, match="matches no projects"):
            resolve_project(conn, "familysearch/web")


def test_the_cli_dry_run_rolls_back_and_the_real_run_writes(tmp_path):
    config = tmp_path / "server.yaml"
    init = CliRunner().invoke(app, ["server", "init", "--config", str(config), "--database-url",
                                    f"sqlite:///{tmp_path / 'db.sqlite'}", "--key-file", str(tmp_path / "key"),
                                    "--no-admin"])
    assert init.exit_code == 0, init.output
    engine = make_engine(load_config(config).database_url)
    with engine.begin() as conn:
        _team(conn)

    def run(*extra):
        return CliRunner().invoke(app, ["server", "import-ops", "--config", str(config), "--workflow", "pr-merged",
                                        "--project", "familysearch/mobile-ios", *extra])

    dry = run("--dry")
    assert dry.exit_code == 0, dry.output
    assert "Dry run, rolled back: workflow pr-merged: created" in dry.output
    assert _counts(engine)["workflows"] == 0
    done = run()
    assert done.exit_code == 0, done.output
    assert "rule github -> pr-merged: created (shadow, dry)" in done.output and "Next: " in done.output
    assert _counts(engine)["workflows"] == 1
    again = run()
    assert "workflow pr-merged: unchanged" in again.output
    with engine.begin() as conn:
        row = conn.execute(select(workflows)).one()
        defs.save(conn, defs.WORKFLOW, row, access=rbac.Access(is_admin=True), body=pr_merged.BODY + "# mine\n",
                  note=None, base_version=1, actor_id=None, now=NOW, ip=None)
    with engine.begin() as conn:
        defs.put_setting(conn, team_id=row.team_id, project_id=row.project_id, key=pr_merged.QA_SETTING,
                         value="In QA", actor_id=None, now=NOW, ip=None)
    edited = run()
    assert edited.exit_code == 1 and "edited since import, pass --force to overwrite" in edited.output
    assert "workflow pr-merged: updated to version 3" in run("--force").output
    missing = CliRunner().invoke(app, ["server", "import-ops", "--config", str(config), "--workflow", "nope",
                                       "--project", "familysearch/mobile-ios"])
    assert missing.exit_code == 1 and "Import refused, nothing changed" in missing.output
    engine.dispose()


@pytest.mark.parametrize("path", ops_harness.fixtures("build-announce") + ops_harness.fixtures("pr-merged"),
                         ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_a_recorded_delivery_makes_the_calls_ops_made(sqlite_engine, world, path, tmp_path, monkeypatch):
    team, ios = world
    workflow, fixture = path.parent.name, ops_harness.load(path)
    repo = (fixture["payload"].get("repository") or {}).get("full_name")
    with sqlite_engine.begin() as conn:
        target = _project(conn, repo) if repo else ios
        for key, value in (fixture.get("settings") or {}).items():
            defs.put_setting(conn, team_id=team, project_id=target, key=key, value=value, actor_id=None, now=NOW,
                             ip=None)
        homes = [_project(conn, r) for r in REPOS] if workflow == "pr-merged" else [ios]
    for home in homes:
        _import(sqlite_engine, team, home, workflow)
    replay = ops_harness.replay(sqlite_engine, team, fixture, tmp_path, monkeypatch)
    ran = fixture["ops"]["result"] is not None
    assert replay.runs == ([(workflow, target)] if ran else []) and len(replay.calls) == len(replay.runs)
    assert replay.effects == ops_harness.expected_effects(fixture)
    assert ops_harness.same_result(fixture["ops"]["result"], replay.result), (fixture["ops"]["result"], replay.result)


def test_each_imported_rule_on_a_webhook_gets_its_own_order_and_keeps_it(sqlite_engine, world):
    team, _ = world
    with sqlite_engine.connect() as conn:
        homes = [_project(conn, r) for r in REPOS]
    for home in homes:
        _import(sqlite_engine, team, home, "pr-merged")
    with sqlite_engine.begin() as conn:
        orders = {r.workflow_id: r.config["order"] for r in conn.execute(select(triggers))}
        hand = conn.execute(select(triggers)).first()
        conn.execute(update(triggers).where(triggers.c.id == hand.id).values(config={**hand.config, "order": 9}))
    assert sorted(orders.values()) == [0, 1, 2]
    for home in homes:
        assert set(_import(sqlite_engine, team, home, "pr-merged").values()) == {"unchanged"}
    with sqlite_engine.connect() as conn:
        assert {r.workflow_id: r.config["order"] for r in conn.execute(select(triggers))} == \
            {**orders, hand.workflow_id: 9}


def test_each_merge_runs_once_in_its_own_projects_copy(sqlite_engine, world, tmp_path, monkeypatch):
    team, ios = world
    with sqlite_engine.connect() as conn:
        android = _project(conn, "fs-eng/android-tree")
    _import(sqlite_engine, team, ios, "pr-merged")
    _import(sqlite_engine, team, android, "pr-merged")
    for name, home in (("story-key-in-branch", android), ("defect-key-in-title", ios),
                       ("key-in-body-already-in-test", None)):
        fixture = ops_harness.load(ops_harness.FIXTURES / f"pr-merged/{name}.json")
        replay = ops_harness.replay(sqlite_engine, team, fixture, tmp_path / name, monkeypatch)
        assert replay.runs == ([("pr-merged", home)] if home else [])


def test_the_harness_sees_a_broken_port(sqlite_engine, world, tmp_path, monkeypatch):
    team, project = world
    monkeypatch.setattr(pr_merged, "BODY", pr_merged.BODY.replace("      defect_resolution: Fixed\n", ""))
    _import(sqlite_engine, team, project, "pr-merged")
    fixture = ops_harness.load(ops_harness.FIXTURES / "pr-merged/defect-key-in-title.json")
    replay = ops_harness.replay(sqlite_engine, team, fixture, tmp_path, monkeypatch)
    assert replay.effects != ops_harness.expected_effects(fixture)
