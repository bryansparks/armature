"""Groundwork for the remaining ops workflows: ported documents and captured ops goldens."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from armature.server import defs, rbac
from armature.server.db import new_id
from armature.server.db.schema import projects, teams
from armature.server.migrate_ops import ported as docs
from armature.server.migrate_ops import upsert_documents
from tests.server import ops_harness

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
REMAINING = ("slack-intake", "repo-review", "code-review", "pr-fix", "development", "ios-port", "localization")
RETIRED = ("talentstorehouse", "mountainriver", "linear-intake")


def test_every_remaining_workflow_names_its_documents_and_roles():
    assert set(docs.NEEDS) == set(REMAINING) == set(docs.ROLES)
    for roles in docs.ROLES.values():
        assert set(roles) <= set(docs.STARTER_ROLES)


def test_the_manifest_matches_the_ported_files_byte_for_byte():
    for ref, entry in docs.manifest().items():
        path = docs.DIR / (ref if "." in ref.rsplit("/", 1)[-1] else f"{ref}.md")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], ref
    on_disk = {str(p.relative_to(docs.DIR)) for p in docs.DIR.rglob("*") if p.is_file()} - {"MANIFEST.json"}
    named = {ref if "." in ref.rsplit("/", 1)[-1] else f"{ref}.md" for ref in docs.manifest()}
    assert on_disk == named


def test_no_document_duplicates_a_starter_role_or_carries_retired_work():
    for workflow in REMAINING:
        for doc in docs.documents(workflow):
            assert doc.key not in docs.STARTER_ROLES and doc.kind != "role"
            assert f"{doc.kind}/{doc.key}" in docs.manifest()
            assert not any(word in doc.body.lower() for word in RETIRED), doc.key


@pytest.mark.parametrize("workflow", REMAINING)
def test_each_document_validates_and_upserts_once_on_the_team(sqlite_engine, workflow):
    for doc in docs.documents(workflow):
        validation = defs.validate_document(doc.kind, doc.body, actor=rbac.Access(is_admin=True), previous_body=None,
                                            key=doc.key)
        assert validation.ok, (doc.key, validation.to_dict())
    team, project = new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        conn.execute(insert(projects).values(id=project, team_id=team, name="mobile-ios", repo="fs-eng/mobile-ios",
                                             created_at=NOW))
        first = upsert_documents(conn, docs.documents(workflow), team_id=team, project_id=project, now=NOW)
        again = upsert_documents(conn, docs.documents(workflow), team_id=team, project_id=project, now=NOW)
    assert {c.action for c in first} == {"created"} and {c.detail for c in first} == {"team"}
    assert {c.action for c in again} == {"unchanged"}


@pytest.mark.parametrize("workflow", REMAINING)
def test_each_workflow_has_captured_ops_goldens(workflow):
    paths = ops_harness.fixtures(workflow)
    assert 2 <= len(paths) <= 4
    for path in paths:
        fixture = json.loads(path.read_text())
        ops = fixture["ops"]
        assert fixture["description"] and {"effects", "sessions", "outputs", "result"} <= set(ops), path.name
        # Sessions quote ops' own prompts verbatim, and those still name retired work, so only the inputs are checked.
        authored = json.dumps({**fixture, "ops": {k: v for k, v in ops.items() if k != "sessions"}}).lower()
        assert not any(word in authored for word in RETIRED), path.name
        whole = json.dumps(fixture)
        assert "/users/stephan" not in whole.lower() and "/private/var" not in whole, path.name
        for session in ops["sessions"]:
            if session["kind"] == "claude":
                assert session["args"][0] == "-p" and session["args"][1], path.name
            else:
                assert session["prompt"] and session["stage"] in fixture.get("stubs", {}), path.name
