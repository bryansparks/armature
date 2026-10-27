"""The response fields the browser code reads, pinned against what the routes return."""
import hashlib
import hmac
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import findings, run_artifacts, runs, workflows
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
                                     "subject", "title", "verdict", "pr", "outcome", "trigger"}),
    ("views/runs.js", "trigger", "run_trigger", {"label", "detail"}),
    ("views/runs.js", "page", "runs", {"next_before"}),
    ("tail.js", "detail", "run_detail", {"run", "steps"}),
    ("tail.js", "run", "run_detail_run", {"status", "started_at", "finished_at", "last_seq"}),
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
    ("views/webhooks.js", "webhook", "webhook", {"id", "team_id", "name", "source", "url", "enabled", "secret_set",
                                                 "secret_name", "forward_url", "generic_auth",
                                                 "generic_signature_header", "generic_delivery_header",
                                                 "last_delivery"}),
    ("views/webhooks.js", "rule", "webhook_rule", {"id", "workflow_id", "workflow_name", "enabled", "config"}),
    ("views/webhooks.js", "workflow", "webhook_workflow", {"id", "name", "scope_kind"}),
    ("views/webhooks.js", "delivery", "delivery", {"id", "received_at", "event", "status", "reason", "matched",
                                                   "forward_status", "headers", "payload"}),
    ("views/webhooks.js", "entry", "delivery_match", {"workflow_name", "workflow_id", "shadow", "run_id",
                                                      "job_status"}),
    ("views/webhooks.js", "decision", "decision", {"trigger_id", "matched", "reason", "inputs"}),
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
    hook = client.post(f"/api/teams/{side.team}/webhooks", json={"name": "gh", "source": "github"},
                       headers=mutation(csrf)).json()["webhook"]
    client.post(f"/api/webhooks/{hook['id']}/secret", json={"value": "s"}, headers=mutation(csrf))
    with sqlite_engine.connect() as conn:
        workflow_id = conn.execute(select(workflows.c.id).where(workflows.c.team_id == side.team)).scalar()
    client.post(f"/api/webhooks/{hook['id']}/rules", json={"workflow_id": workflow_id, "config": {}},
                headers=mutation(csrf))
    raw = b'{"n": 1}'
    client.post(hook["url"].removeprefix(ORIGIN), content=raw, headers={
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"s", raw, hashlib.sha256).hexdigest(), "X-GitHub-Event": "push"})
    hook_detail = client.get(f"/api/webhooks/{hook['id']}").json()
    delivery_id = client.get(f"/api/webhooks/{hook['id']}/deliveries").json()["deliveries"][0]["id"]
    delivery = client.get(f"/api/webhooks/{hook['id']}/deliveries/{delivery_id}").json()["delivery"]
    decisions = client.post(f"/api/webhooks/{hook['id']}/deliveries/{delivery_id}/test", json={},
                            headers=mutation(csrf)).json()["decisions"]
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
        "run_trigger": client.get("/api/runs").json()["runs"][0]["trigger"],
        "run_detail": client.get(f"/api/runs/{run_id}").json(),
        "run_detail_run": client.get(f"/api/runs/{run_id}").json()["run"],
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
        "webhook": client.get(f"/api/teams/{side.team}/webhooks").json()["webhooks"][0],
        "webhook_rule": hook_detail["rules"][0],
        "webhook_workflow": hook_detail["workflows"][0],
        "delivery": delivery,
        "delivery_match": delivery["matched"][0],
        "decision": decisions[0],
    }


@pytest.mark.parametrize("path,variable,response,fields", READS, ids=[f"{r[0]}:{r[1]}" for r in READS])
def test_browser_reads_fields_the_route_returns(responses, path, variable, response, fields):
    read = _read_fields((STATIC / path).read_text(), variable)
    assert fields <= read, f"{path} no longer reads {sorted(fields - read)} from {variable}; update READS"
    returned = set(responses[response])
    assert read <= returned | {"length"}, f"{path} reads {sorted(read - returned)} from {variable}, not returned"
