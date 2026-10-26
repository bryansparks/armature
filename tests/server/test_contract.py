"""The response fields the browser code reads, pinned against what the routes return."""
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import insert

from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import findings, run_artifacts, runs
from tests.server.app_support import (
    ORIGIN,
    Clock,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.test_rbac import _side

STATIC = Path(__file__).resolve().parents[2] / "armature" / "server" / "static"

# (file, variable the file reads the response into, route the response comes from, fields read)
READS = [
    ("invite.js", "invite", "inspect", {"username", "otpauth_uri"}),
    ("views/users.js", "created", "create_user", {"invite_url"}),
    ("views/users.js", "issued", "reissue", {"invite_url"}),
    ("views/users.js", "account", "user", {"id", "username", "display_name", "email", "is_admin", "disabled", "sso",
                                           "totp_confirmed", "memberships"}),
    ("views/users.js", "membership", "membership", {"id", "scope_kind", "scope_id", "scope_name", "role"}),
    ("views/users.js", "match", "lookup", {"id", "username"}),
    ("session.js", "current", "session", {"user", "memberships", "sso"}),
    ("login.js", "sessionPayload", "session", {"user", "csrf", "auth"}),
    ("views/runs.js", "run", "run", {"cost_usd", "finished_at", "started_at", "status", "triggered_by", "workflow",
                                     "subject", "title", "verdict", "pr", "outcome"}),
    ("views/runs.js", "page", "runs", {"next_before"}),
    ("views/secrets.js", "secret", "secret", {"name", "set", "updated_at", "updated_by"}),
    ("views/teams.js", "projectRecord", "project", {"name", "platform", "team_id", "id"}),
    ("views/teams.js", "project", "project", {"name", "platform", "id"}),
    ("views/teams.js", "team", "team", {"id", "name"}),
    ("views/runnerkeys.js", "key", "runner_key", {"id", "name", "created_at", "created_by", "last_used_at"}),
    ("views/runnerkeys.js", "created", "create_runner_key", {"key"}),
    ("views/run.js", "artifact", "artifact", {"media_type", "content", "size", "created_at", "step_id"}),
    ("views/overview.js", "overview", "overview", {"running", "alerts", "recent", "reviews_today",
                                                   "reevaluations_today", "timezone"}),
    ("views/trends.js", "trend", "trends", {"workflows", "days", "runs_by_workflow", "spend_by_workflow",
                                            "failure_rate", "verdicts_per_day",
                                            "man_hours_per_day", "agent_hours_per_day", "slowest_steps",
                                            "cost_by_role", "timezone"}),
    ("views/subject.js", "row", "subject_row", {"subject", "title", "status", "runs", "reviews", "fixes", "cost_usd",
                                                "last_activity"}),
    ("views/subject.js", "page", "subjects", {"next_before"}),
    ("views/subject.js", "detail", "subject", {"subject", "title", "status", "latest_verdict", "repo", "links", "runs",
                                               "findings"}),
    ("views/subject.js", "run", "subject_run", {"id", "kind", "workflow", "workflow_label", "status", "started_at",
                                                "verdict", "mode", "head", "cost_usd", "artifacts", "fix", "merge",
                                                "fields"}),
    ("views/subject.js", "finding", "subject_finding", {"finding_key", "title", "path", "line",
                                                        "current_disposition", "history"}),
    ("views/subject.js", "event", "finding_event", {"run_id", "started_at", "disposition"}),
]


def _read_fields(source: str, variable: str) -> set[str]:
    return set(re.findall(rf"\b{variable}\??\.([a-z_]+)\b", source))


@pytest.fixture
def responses(sqlite_engine, keyring):
    clock = Clock()
    with sqlite_engine.begin() as conn:
        side = _side(conn, "own", clock)
        secret_store.set_secret(conn, keyring, team_id=side.team, name="K", value="v", actor_id=None)
        run_id = new_id()
        conn.execute(insert(runs).values(id=run_id, team_id=side.team, status="running", started_at=clock(),
                                         cost_usd=Decimal(1)))
        conn.execute(insert(run_artifacts).values(run_id=run_id, name="review.md", step_id="post",
                                                  media_type="text/markdown", content="# Review", size=8,
                                                  created_at=clock()))
        subject_run = new_id()
        conn.execute(insert(runs).values(id=subject_run, team_id=side.team, status="succeeded", started_at=clock(),
                                         workflow_name="pr-fix", subject="acme/app#1", summary={"title": "T"},
                                         trigger_ref={"head": "abc", "fix": {"outcome": "pushed"}}))
        conn.execute(insert(findings).values(id=new_id(), run_id=subject_run, finding_key="k", disposition="keep",
                                             created_at=clock()))
    admin_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, admin_id)
    created = client.post("/api/users", json={"username": "bea", "team_id": side.team, "role": "viewer"},
                          headers=mutation(csrf)).json()
    token = created["invite_url"].split("#", 1)[1]
    guest = make_client(sqlite_engine, keyring, clock)
    guest_csrf = guest.get("/api/session").json()["csrf"]
    users = client.get("/api/users").json()["users"]
    subject = client.get("/api/subjects/acme%2Fapp%231").json()["subject"]
    created_key = client.post("/api/runner-keys", json={"name": "host"}, headers=mutation(csrf)).json()
    return {
        "inspect": guest.post("/api/invite/inspect", json={"token": token},
                              headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf}).json(),
        "create_user": created,
        "reissue": client.post(f"/api/users/{created['user']['id']}/invite", json={}, headers=mutation(csrf)).json(),
        "user": users[0],
        "membership": next(u for u in users if u["memberships"])["memberships"][0],
        "lookup": client.get("/api/users/lookup", params={"q": "bea"}).json()["users"][0],
        "session": client.get("/api/session").json(),
        "run": client.get("/api/runs").json()["runs"][0],
        "runs": client.get("/api/runs").json(),
        "secret": client.get(f"/api/teams/{side.team}/secrets").json()["secrets"][0],
        "project": client.get(f"/api/projects/{side.project}").json()["project"],
        "team": client.get("/api/teams").json()["teams"][0],
        "create_runner_key": created_key,
        "runner_key": client.get("/api/runner-keys").json()["runner_keys"][0],
        "artifact": client.get(f"/api/runs/{run_id}/artifacts/review.md").json()["artifact"],
        "overview": client.get("/api/overview").json(),
        "trends": client.get("/api/trends").json(),
        "subjects": client.get("/api/subjects").json(),
        "subject_row": client.get("/api/subjects").json()["subjects"][0],
        "subject": subject,
        "subject_run": subject["runs"][0],
        "subject_finding": subject["findings"][0],
        "finding_event": subject["findings"][0]["history"][0],
    }


@pytest.mark.parametrize("path,variable,response,fields", READS, ids=[f"{r[0]}:{r[1]}" for r in READS])
def test_browser_reads_fields_the_route_returns(responses, path, variable, response, fields):
    read = _read_fields((STATIC / path).read_text(), variable)
    assert fields <= read, f"{path} no longer reads {sorted(fields - read)} from {variable}; update READS"
    returned = set(responses[response])
    assert read <= returned | {"length"}, f"{path} reads {sorted(read - returned)} from {variable}, not returned"
