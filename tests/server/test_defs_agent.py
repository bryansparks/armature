"""Skill documents and agent: steps in stored definitions."""
from __future__ import annotations

import pytest

from armature.server import defs, rbac
from armature.server.export import DOCUMENT_DIRS

TEAM, PROJECT = "team-1", "project-1"
WRITE_BODY = """name: fixer
stages:
  - id: fix
    agent:
      model: review
      access: write
      prompt: Fix it.
"""
SKILL = "---\nname: kotlin-review\ndescription: Review Kotlin changes.\n---\nLook for coroutine leaks.\n"


def access(team_role=None, project_role=None, admin=False):
    return rbac.Access(is_admin=admin, teams={TEAM: team_role} if team_role else {},
                       projects={PROJECT: project_role} if project_role else {},
                       project_teams={PROJECT: TEAM})


def codes(validation):
    return {e["code"] for e in validation.errors}


@pytest.mark.parametrize("who, scope, ok", [
    (access(admin=True), defs.DefScope("team", TEAM), True),
    (access("team_lead"), defs.DefScope("team", TEAM), True),
    (access(project_role="project_lead"), defs.DefScope("project", TEAM, PROJECT), True),
    (access(project_role="contributor"), defs.DefScope("project", TEAM, PROJECT), False),
])
def test_access_write_needs_a_project_lead(who, scope, ok):
    validation = defs._validate(defs.WORKFLOW, WRITE_BODY, access=who, previous_body=WRITE_BODY, doc_kind=None,
                                catalog=object(), scope=scope, key="fixer")
    assert ("AGENT_WRITE_ROLE" not in codes(validation)) is ok


def test_read_access_needs_no_lead():
    body = WRITE_BODY.replace("access: write", "access: read")
    validation = defs._validate(defs.WORKFLOW, body, access=access(project_role="contributor"), previous_body=body,
                                doc_kind=None, catalog=object(), scope=defs.DefScope("project", TEAM, PROJECT),
                                key="fixer")
    assert "AGENT_WRITE_ROLE" not in codes(validation)


def test_a_skill_needs_a_name_matching_its_key_and_a_description():
    admin = access(admin=True)
    assert defs.validate_document("skill", SKILL, actor=admin, previous_body=None, key="kotlin-review").ok
    assert "SKILL_NAME" in codes(defs.validate_document("skill", SKILL, actor=admin, previous_body=None, key="other"))
    assert "SKILL_NAME" in codes(defs.validate_document("skill", SKILL.replace("kotlin-review", "Kotlin Review"),
                                                       actor=admin, previous_body=None))
    no_description = SKILL.replace("description: Review Kotlin changes.\n", "")
    assert "SKILL_DESCRIPTION" in codes(defs.validate_document("skill", no_description, actor=admin,
                                                              previous_body=None))


def test_a_skill_is_at_most_64_kb():
    big = SKILL + "x" * defs.SKILL_MAX
    assert "TOO_LARGE" in codes(defs.validate_document("skill", big, actor=access(admin=True), previous_body=None))


def test_skills_export_to_their_own_directory():
    assert DOCUMENT_DIRS["skill"] == "skills"


@pytest.mark.parametrize("role, code", [("team_lead", None), ("'{{ specialist }}'", "AGENT_TEMPLATE"),
                                        ("a/b", "AGENT_ROLE_NAME")])
def test_an_agent_role_is_a_plain_role_document_key(role, code):
    body = WRITE_BODY.replace("      access: write\n", f"      role: {role}\n")
    found = codes(defs.validate_workflow(body, actor=access(admin=True), previous_body=None, catalog=object()))
    assert (code in found) if code else not found


def test_a_role_is_capped_like_a_skill():
    big = "---\nname: big\n---\n" + "x" * defs.SKILL_MAX
    assert "TOO_LARGE" in codes(defs.validate_document("role", big, actor=access(admin=True), previous_body=None))
    assert defs.validate_document("role", "---\nname: ok\n---\nBe brief.", actor=access(admin=True),
                                  previous_body=None).ok
