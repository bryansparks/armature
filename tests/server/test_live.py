"""Runner keys, registration, event ingest, the writer, and the live streams, on a real server."""
import asyncio
import os
import stat
import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select, update

from armature.events.emitter import Emitter
from armature.server import runners
from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    candidate_matches,
    finding_candidates,
    findings,
    llm_calls,
    memberships,
    projects,
    run_events,
    run_steps,
    run_tokens,
    runs,
    teams,
    tool_calls,
    workflows,
)
from armature.server.ingest import MAX_CANDIDATES, candidate_row, llm_call_row
from armature.server.tools.review import finding_key
from armature.server.writer import RunChange, _apply_run
from tests.server.app_support import Clock, add_membership, add_user
from tests.server.live_support import (
    SSE,
    LiveServer,
    add_runner_key,
    add_team,
    line,
    post,
    register,
)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(engine, keyring, clock):
    team, project = add_team(engine, clock, "mobile")
    other, other_project = add_team(engine, clock, "web")
    users = {
        "admin": add_user(engine, keyring, clock, "root", is_admin=True)[0],
        "lead": add_user(engine, keyring, clock, "lead")[0],
        "viewer": add_user(engine, keyring, clock, "viewer")[0],
        "outsider": add_user(engine, keyring, clock, "outsider")[0],
        "other_lead": add_user(engine, keyring, clock, "otherlead")[0],
    }
    add_membership(engine, clock, users["lead"], "team", team, "team_lead")
    add_membership(engine, clock, users["viewer"], "team", team, "viewer")
    add_membership(engine, clock, users["outsider"], "team", other, "viewer")
    add_membership(engine, clock, users["other_lead"], "team", other, "team_lead")
    return {"team": team, "project": project, "other": other, "other_project": other_project, "users": users,
            "key": add_runner_key(engine, keyring, clock, team),
            "other_key": add_runner_key(engine, keyring, clock, other)}


@pytest.fixture
def live(engine, keyring, clock):
    with LiveServer(engine, keyring, clock) as server:
        yield server


def _count(engine, query):
    with engine.connect() as conn:
        return conn.execute(query).scalar()


def _stored(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(run_events).where(run_events.c.run_id == run_id)
                            .order_by(run_events.c.seq)).all()


def test_runner_key_routes(live, world):
    lead = live.client(world["users"]["lead"])
    url = f"/api/teams/{world['team']}/runner-keys"
    created = lead.post(url, json={"name": "build box"}, headers=live.csrf(lead))
    assert created.status_code == 200
    body = created.json()
    key, record = body["key"], body["runner_key"]
    assert key.startswith("ark_") and set(record) == {"id", "name", "created_at", "created_by", "last_used_at"}
    listed = lead.get(url).json()["runner_keys"]
    assert any(k["id"] == record["id"] for k in listed) and key not in str(listed)

    anonymous = live.client()
    assert anonymous.post("/api/runner/runs", json={"workflow": "w"},
                          headers={"Authorization": "Bearer ark_" + "x" * 43}).status_code == 401
    assert anonymous.post("/api/runner/runs", json={"workflow": "w"}).status_code == 401
    register(anonymous, key)

    assert lead.post(f"{url}/{record['id']}/delete", headers=live.csrf(lead)).json() == {"ok": True}
    assert anonymous.post("/api/runner/runs", json={"workflow": "w"},
                          headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert all(k["id"] != record["id"] for k in lead.get(url).json()["runner_keys"])
    assert lead.post(f"{url}/{record['id']}/delete", headers=live.csrf(lead)).status_code == 404

    no_step_up = live.client(world["users"]["lead"], step_up=False)
    assert no_step_up.post(url, json={"name": "x"}, headers=live.csrf(no_step_up)).status_code == 403
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(url).status_code == 403
    outsider = live.client(world["users"]["other_lead"])
    assert outsider.get(url).status_code == 404
    assert outsider.post(url, json={"name": "x"}, headers=live.csrf(outsider)).status_code == 404


def test_registration_shape_and_validation(live, world):
    http = live.client()
    body = register(http, world["key"], project="mobile-app")
    assert set(body) == {"run_id", "token", "events"}
    assert body["events"] == {"unix": live.hub.socket_path, "http": f"{live.origin}/api/events"}
    with live.engine.connect() as conn:
        row = conn.execute(select(runs).where(runs.c.id == body["run_id"])).one()
    assert (row.team_id, row.project_id, row.status, row.workflow_name, row.actor, row.trigger_ref) == \
        (world["team"], world["project"], "running", "pr-review", "octocat", {"pr": 1, "source": "runner"})
    auth = {"Authorization": f"Bearer {world['key']}"}
    assert http.post("/api/runner/runs", json={"workflow": "w", "project": "web-app"}, headers=auth).status_code == 404
    assert http.post("/api/runner/runs", json={"workflow": "w", "rerun_of": "nope"}, headers=auth).status_code == 404
    assert http.post("/api/runner/runs", json={"actor": "a"}, headers=auth).status_code == 400
    assert http.post("/api/runner/runs", json=[1], headers=auth).status_code == 400
    rerun = http.post("/api/runner/runs", json={"workflow": "w", "rerun_of": body["run_id"]}, headers=auth)
    assert rerun.status_code == 200


def test_instance_runner_key_routes(live, world):
    admin = live.client(world["users"]["admin"])
    created = admin.post("/api/runner-keys", json={"name": "m1 host"}, headers=live.csrf(admin))
    assert created.status_code == 200
    key, record = created.json()["key"], created.json()["runner_key"]
    assert key.startswith("ark_") and set(record) == {"id", "name", "created_at", "created_by", "last_used_at"}
    assert [k["id"] for k in admin.get("/api/runner-keys").json()["runner_keys"]] == [record["id"]]
    team_url = f"/api/teams/{world['team']}/runner-keys"
    assert record["id"] not in str(admin.get(team_url).json())

    lead = live.client(world["users"]["lead"])
    assert lead.get("/api/runner-keys").status_code == 403
    assert lead.post("/api/runner-keys", json={"name": "x"}, headers=live.csrf(lead)).status_code == 403
    assert lead.post(f"/api/runner-keys/{record['id']}/delete", headers=live.csrf(lead)).status_code == 403
    assert lead.post(f"{team_url}/{record['id']}/delete", headers=live.csrf(lead)).status_code == 404
    no_step_up = live.client(world["users"]["admin"], step_up=False)
    assert no_step_up.post("/api/runner-keys", json={"name": "x"}, headers=live.csrf(no_step_up)).status_code == 403
    assert no_step_up.post(f"/api/runner-keys/{record['id']}/delete",
                           headers=live.csrf(no_step_up)).status_code == 403

    http = live.client()
    run = register(http, key)
    assert post(http, run["token"], line("log", None, text="before"))["accepted"] == 1
    assert admin.post(f"/api/runner-keys/{record['id']}/delete", headers=live.csrf(admin)).json() == {"ok": True}
    assert post(http, run["token"], line("log", None, text="after"))["rejected"] == 1
    assert _count(live.engine, select(func.count()).select_from(run_tokens)
                  .where(run_tokens.c.run_id == run["run_id"])) == 0
    assert http.post("/api/runner/runs", json={"workflow": "w"},
                     headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert admin.get("/api/runner-keys").json()["runner_keys"] == []
    assert admin.post(f"/api/runner-keys/{record['id']}/delete", headers=live.csrf(admin)).status_code == 404


def test_instance_key_registers_into_the_repositorys_team(live, world, keyring, clock):
    with live.engine.begin() as conn:
        conn.execute(update(projects).where(projects.c.id == world["project"]).values(repo="fs-eng/android-tree"))
        conn.execute(update(projects).where(projects.c.id == world["other_project"]).values(repo="fs-eng/web"))
        conn.execute(update(teams).where(teams.c.id == world["other"]).values(slug="web"))
        workflow_id = new_id()
        conn.execute(insert(workflows).values(id=workflow_id, team_id=world["team"], name="pr-review",
                                              current_version=1, created_at=clock(), updated_at=clock()))
        _, key = runners.create_key(conn, keyring, team_id=None, name="host", actor_id=None, now=clock(), ip=None)
    http = live.client()

    def landed(run):
        with live.engine.connect() as conn:
            row = conn.execute(select(runs).where(runs.c.id == run["run_id"])).one()
        return row.team_id, row.project_id, row.workflow_id

    assert landed(register(http, key, project="FS-Eng/Android-Tree")) == \
        (world["team"], world["project"], workflow_id)
    assert landed(register(http, key, project="fs-eng/web")) == (world["other"], world["other_project"], None)
    assert landed(register(http, key, project="web/web-app")) == (world["other"], world["other_project"], None)

    unknown = landed(register(http, key, project="fs-eng/nowhere"))
    with live.engine.connect() as conn:
        ops = conn.execute(select(teams).where(teams.c.slug == "operations")).one()
    assert ops.name == "Operations (unassigned runs)"
    assert unknown == (ops.id, None, None)
    assert landed(register(http, key)) == (ops.id, None, None)
    assert landed(register(http, key, project="mobile/mobile-app")) == (ops.id, None, None)
    assert _count(live.engine, select(func.count()).select_from(teams).where(teams.c.slug == "operations")) == 1
    assert _count(live.engine, select(func.count()).select_from(audit)
                  .where(audit.c.action == "team.create", audit.c.team_id == ops.id)) == 1
    assert _count(live.engine, select(func.count()).select_from(memberships)
                  .where(memberships.c.scope_id == ops.id)) == 0

    admin = live.client(world["users"]["admin"])
    assert ops.id in {t["id"] for t in admin.get("/api/teams").json()["teams"]}
    lead = live.client(world["users"]["lead"])
    assert ops.id not in {t["id"] for t in lead.get("/api/teams").json()["teams"]}

    auth = {"Authorization": f"Bearer {world['key']}"}
    assert http.post("/api/runner/runs", json={"workflow": "w", "project": "fs-eng/web"},
                     headers=auth).status_code == 404


def test_ingest_writes_summary_on_run_end(live, world, keyring, clock):
    with live.engine.begin() as conn:
        conn.execute(insert(workflows).values(id=new_id(), team_id=world["team"], name="pr-review", kind="review",
                                              current_version=1, created_at=clock(), updated_at=clock()))
    http = live.client()
    run = register(http, world["key"])
    token = run["token"]
    result = post(http, token,
                  line("step_start", "review"),
                  line("cost", "review", model="m", input_tokens=100, output_tokens=20, cost_usd=0.25),
                  line("cost", "sub-1", parent_step="review", input_tokens=5, output_tokens=1, cost_usd=0.5),
                  line("finding", "review", path="a.py", line=3, severity="high", body="bad"),
                  line("step_end", "review"),
                  line("step_start", "build"),
                  line("step_failed", "build", reason="exit 1"),
                  line("run_end", None, status="failed", error="build broke", reported_cost_usd=99))
    assert result == {"accepted": 8, "rejected": 0, "duplicates": 0}
    live.flush()
    viewer = live.client(world["users"]["viewer"])
    detail = viewer.get(f"/api/runs/{run['run_id']}").json()
    summary = detail["run"]
    assert set(summary) == {"id", "team_id", "project_id", "workflow", "status", "triggered_by", "trigger_kind",
                            "started_at", "finished_at", "cost_usd", "input_tokens", "output_tokens", "rerun_of",
                            "rerun", "can_rerun",
                            "cancelled_by", "error", "subject", "thread", "summary", "last_seq", "dry_run", "kind",
                            "links", "trigger", "outcome", "outcome_source", "risk", "risk_reasons",
                            "head_sha", "location", "review_round", "prev_round_run_id", "review_kind", "host_run"}
    assert summary["dry_run"] is False and summary["kind"] == "review"
    assert (summary["status"], summary["error"], summary["workflow"], summary["triggered_by"]) == \
        ("failed", "build broke", "pr-review", "octocat")
    assert summary["cost_usd"] == pytest.approx(0.75) and summary["input_tokens"] == 105
    steps = {s["step_id"]: s for s in detail["steps"]}
    assert steps["review"]["status"] == "succeeded" and steps["review"]["cost_usd"] == pytest.approx(0.25)
    assert steps["build"]["status"] == "failed" and steps["build"]["error"] == "exit 1"
    assert steps["sub-1"]["parent_step"] == "review"
    with live.engine.connect() as conn:
        db_run = conn.execute(select(runs).where(runs.c.id == run["run_id"])).one()
        assert db_run.status == "failed" and db_run.finished_at is not None
        assert float(db_run.cost_usd) == pytest.approx(0.75) and db_run.output_tokens == 21
        assert conn.execute(select(findings.c.path, findings.c.line)).all() == [("a.py", 3)]
        assert conn.execute(select(func.count()).select_from(run_tokens)
                            .where(run_tokens.c.run_id == run["run_id"])).scalar() == 0
    assert post(http, token, line("log", None, text="late")) == {"accepted": 0, "rejected": 1, "duplicates": 0}
    listed = viewer.get("/api/runs", params={"status": "failed"}).json()["runs"]
    assert listed[0]["id"] == run["run_id"] and listed[0]["workflow"] == "pr-review"


def test_cost_events_record_what_ran_once_each_and_the_run_detail_shows_it(live, world, clock):
    http = live.client()
    run = register(http, world["key"])
    attempts = [{"tier": "small", "model": "gpt-4o-mini", "input_tokens": 50, "output_tokens": 10, "cost_usd": 0.001,
                 "outcome": "parse_error"},
                {"tier": "large", "model": "anthropic/claude-opus-5-5", "input_tokens": 60, "output_tokens": 12,
                 "cost_usd": 0.02, "outcome": "ok"}]
    usage = {"claude-sonnet-5-5": {"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.4, "junk": "x"}}
    post(http, run["token"],
         line("step_start", "judge"),
         line("cost", "judge", model="anthropic/claude-opus-5-5", input_tokens=60, output_tokens=12, cost_usd=0.021,
              tier="large", provider="anthropic", requested_effort="high", attempts=attempts, escalation_index=1,
              cached=True),
         line("step_start", "review"),
         line("cost", "review", model="claude-sonnet-5-5", input_tokens=10, output_tokens=2, cost_usd=0.4,
              effort="medium", requested_effort="high", provider="anthropic", alias="sonnet", model_usage=usage),
         line("cost", "review", model="claude-opus-5-5", input_tokens=10, output_tokens=2, cost_usd=0.1,
              effort="xhigh", requested_effort="max", provider="bedrock", alias="opus"),
         line("step_start", "build"))
    live.flush()
    with live.engine.connect() as conn:
        rows = conn.execute(select(llm_calls).where(llm_calls.c.run_id == run["run_id"])
                            .order_by(llm_calls.c.seq)).all()
    assert [(r.seq, r.step_key, r.team_id) for r in rows] == [
        (2, "judge", world["team"]), (4, "review", world["team"]), (5, "review", world["team"])]
    judge, review, _ = rows
    assert (judge.model_id, judge.provider, judge.tier, judge.requested_effort, judge.reported_effort,
            judge.escalation_index, judge.cached) == ("anthropic/claude-opus-5-5", "anthropic", "large", "high",
                                                      None, 1, True)
    assert float(judge.cost_usd) == pytest.approx(0.021) and judge.input_tokens == 60
    assert [a["outcome"] for a in judge.attempts] == ["parse_error", "ok"]
    assert (review.alias, review.requested_effort, review.reported_effort, review.cached) == (
        "sonnet", "high", "medium", False)
    assert review.model_usage == {"claude-sonnet-5-5": {"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.4}}

    replay = RunChange(llm_calls=[
        llm_call_row(world["team"], seq, "judge", clock(), {"model": "other"}, 9.0, 1, 1) for seq in (2, 99)])
    with live.engine.begin() as conn:
        _apply_run(conn, run["run_id"], replay)
    with live.engine.connect() as conn:
        stored = {r.seq: r for r in conn.execute(select(llm_calls).where(llm_calls.c.run_id == run["run_id"]))}
    assert sorted(stored) == [2, 4, 5, 99]
    assert stored[2].model_id == "anthropic/claude-opus-5-5" and float(stored[2].cost_usd) == pytest.approx(0.021)
    assert stored[99].model_id == "other"

    steps = {s["step_id"]: s for s in live.client(world["users"]["viewer"])
             .get(f"/api/runs/{run['run_id']}").json()["steps"]}
    fields = ("model", "provider", "alias", "tier", "requested_effort", "reported_effort")
    assert {k: steps["review"][k] for k in fields} == {
        "model": "claude-opus-5-5", "provider": "bedrock", "alias": "opus", "tier": None, "requested_effort": "max",
        "reported_effort": "xhigh"}
    assert {k: steps["build"][k] for k in fields} == dict.fromkeys(fields)


def test_llm_call_row_caps_hostile_payloads():
    payload = {"model": "m\x00x", "provider": "\x00", "alias": "a\x00b", "tier": "t\x00",
               "attempts": [{"tier": "small", "model": "x\x00y", "input_tokens": True, "output_tokens": 2 ** 40,
                             "outcome": "ok"}] * 40,
               "model_usage": {f"m{i}\x00": {"input_tokens": 1, "cost_usd": 0.1} for i in range(40)}}
    now = datetime(2026, 11, 13, tzinfo=UTC)
    row = llm_call_row("team", 1, "step\x00", now, {**payload, "escalation_index": True}, 0.0, 0, 0)
    assert len(row["attempts"]) == 16 and len(row["model_usage"]) == 16
    assert row["escalation_index"] is None
    assert (row["model_id"], row["provider"], row["alias"], row["tier"], row["step_key"]) == (
        "mx", None, "ab", "t", "step")
    assert row["attempts"][0]["model"] == "xy" and row["attempts"][0]["input_tokens"] == 0
    assert "m0" in row["model_usage"] and row["cached"] is False
    assert llm_call_row("team", 1, None, now, {"escalation_index": 2 ** 40}, 0.0, 0, 0)["escalation_index"] is None
    assert llm_call_row("team", 1, None, now, {"escalation_index": 2}, 0.0, 0, 0)["escalation_index"] == 2


def test_token_is_bound_to_its_run(live, world):
    http = live.client()
    first, second = register(http, world["key"]), register(http, world["other_key"])
    assert post(http, first["token"], line("log", None, run_id=second["run_id"], team_id=world["other"],
                                           token="leak", text="hi"))["accepted"] == 1
    line_token = line("log", None, event_token=first["token"], text="body token")
    assert post(http, second["token"], line_token)["accepted"] == 1
    live.flush()
    stored = _stored(live.engine, first["run_id"])
    assert [r.payload for r in stored] == [{"text": "hi"}, {"text": "body token"}]
    assert _stored(live.engine, second["run_id"]) == []


def test_bad_tokens_and_oversize_refused(live, world, clock):
    http = live.client()
    run = register(http, world["key"])
    assert post(http, "x" * 43, line("log", None, text="a"))["rejected"] == 1
    assert post(http, run["token"], line("log", None, text="a" * (65 * 1024)))["rejected"] == 1
    assert post(http, run["token"], b"not json", line("log", None, text="ok"),
                b'{"v": 2, "kind": "log"}') == {"accepted": 1, "rejected": 2, "duplicates": 0}
    clock.advance(hours=49)
    live.hub._tokens.clear()
    assert post(http, run["token"], line("log", None, text="late"))["rejected"] == 1
    too_big = http.post("/api/events", content=b"x" * (1024 * 1024 + 1),
                        headers={"Authorization": f"Bearer {run['token']}"})
    assert too_big.status_code == 413


def test_team_secret_redacted_in_tool_result(live, world, keyring):
    with live.engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=world["team"], name="DEPLOY_KEY", value="hunter2-deploy-key",
                                actor_id=None)
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("tool_result", "s", tool="Bash", content="cat env: DEPLOY_KEY=hunter2-deploy-key"))
    live.flush()
    stored = _stored(live.engine, run["run_id"])
    assert stored[0].payload["content"] == "cat env: DEPLOY_KEY=[redacted]"
    assert "hunter2" not in str(live.call(live.hub.events_after(run["run_id"], 0, 10)))


def test_seq_is_monotonic_under_concurrent_ingest(live, world):
    http = live.client()
    run = register(http, world["key"])
    emitter = Emitter(f"unix:{live.hub.socket_path}", run["token"])

    def via_http(worker):
        with live.client() as own:
            for batch in range(5):
                post(own, run["token"], *[line("log", None, w=worker, b=batch, i=i) for i in range(20)])

    threads = [threading.Thread(target=via_http, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for i in range(200):
        emitter.emit("log", None, i=i)
    for t in threads:
        t.join()
    assert emitter.flush(5)
    deadline = time.monotonic() + 5
    while live.hub.accepted < 600 and time.monotonic() < deadline:
        time.sleep(0.02)
    live.flush()
    assert [r.seq for r in _stored(live.engine, run["run_id"])] == list(range(1, 601))
    emitter.close()


def test_socket_is_private(live):
    mode = os.stat(live.hub.socket_path).st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o600


def test_sse_replay_live_and_resume(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], *[line("log", None, i=i) for i in range(5)])
    live.flush()
    post(http, run["token"], line("log", None, i=5))
    viewer = live.client(world["users"]["viewer"])
    url = f"/api/runs/{run['run_id']}/stream"
    with viewer.stream("GET", url) as raw:
        response = SSE(raw)
        assert raw.headers["content-type"].startswith("text/event-stream")
        frames = response.until(lambda f: sum(x.get("event") == "run-event" for x in f) >= 6)
        assert frames[0].get("retry") and frames[1]["event"] == "run"
        events = [f for f in frames if f.get("event") == "run-event"]
        assert [int(f["id"]) for f in events] == [1, 2, 3, 4, 5, 6]
        assert set(events[0]["data"]) == {"seq", "ts", "kind", "step_id", "payload"}
        post(http, run["token"], line("log", None, i=6), line("cost", None, cost_usd=1.0))
        frames = response.until(lambda f: any(x.get("event") == "run" for x in f))
        live_events = [f for f in frames if f.get("event") == "run-event"]
        assert int(live_events[0]["id"]) == 7
        assert next(f for f in frames if f.get("event") == "run")["data"]["cost_usd"] == pytest.approx(1.0)
    with viewer.stream("GET", url, headers={"Last-Event-ID": "6"}) as raw:
        response = SSE(raw)
        frames = response.until(lambda f: sum(x.get("event") == "run-event" for x in f) >= 2)
        assert [int(f["id"]) for f in frames if f.get("event") == "run-event"] == [7, 8]
    with viewer.stream("GET", f"{url}?after=7") as raw:
        response = SSE(raw)
        frames = response.until(lambda f: any(x.get("event") == "run-event" for x in f))
        assert [int(f["id"]) for f in frames if f.get("event") == "run-event"] == [8]
    page = viewer.get(f"/api/runs/{run['run_id']}/events", params={"after": 2, "limit": 3}).json()
    assert [e["seq"] for e in page["events"]] == [3, 4, 5] and page["next_after"] == 5
    assert viewer.get(f"/api/runs/{run['run_id']}/events", params={"after": 7}).json()["next_after"] is None
    back = viewer.get(f"/api/runs/{run['run_id']}/events", params={"before": 8, "limit": 3}).json()
    assert [e["seq"] for e in back["events"]] == [5, 6, 7] and back["prev_before"] == 5
    start = viewer.get(f"/api/runs/{run['run_id']}/events", params={"before": back["prev_before"], "limit": 10})
    assert [e["seq"] for e in start.json()["events"]] == [1, 2, 3, 4] and start.json()["prev_before"] is None
    assert viewer.get(f"/api/runs/{run['run_id']}").json()["run"]["last_seq"] == 8
    live.flush()
    live.hub._runs.clear()
    assert viewer.get(f"/api/runs/{run['run_id']}").json()["run"]["last_seq"] == 8
    fresh = register(http, world["key"])
    assert viewer.get(f"/api/runs/{fresh['run_id']}").json()["run"]["last_seq"] == 0


def test_cost_frames_are_throttled(live, world):
    http = live.client()
    run = register(http, world["key"])
    viewer = live.client(world["users"]["viewer"])
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        response = SSE(raw)
        response.until(lambda f: any(x.get("event") == "run" for x in f))
        started = time.monotonic()
        for _ in range(5):
            post(http, run["token"], line("cost", "s", input_tokens=10, output_tokens=1, cost_usd=0.1))
        frames = response.until(lambda f: any(x.get("event") == "run" and x["data"]["input_tokens"] == 50
                                              for x in f))
        runs_seen = [f["data"] for f in frames if f.get("event") == "run"]
        assert len(runs_seen) <= 3 and runs_seen[-1]["cost_usd"] == pytest.approx(0.5)
        assert time.monotonic() - started < 3


def test_runs_stream_is_scoped(live, world):
    http = live.client()
    outsider = live.client(world["users"]["outsider"])
    viewer = live.client(world["users"]["viewer"])
    with viewer.stream("GET", "/api/stream/runs") as mine_raw, \
            outsider.stream("GET", "/api/stream/runs") as theirs_raw:
        mine, theirs = SSE(mine_raw), SSE(theirs_raw)
        mine.until(lambda f: True)
        theirs.until(lambda f: True)
        run = register(http, world["key"])
        other_run = register(http, world["other_key"])
        post(http, run["token"], line("run_end", None, status="succeeded"))
        mine_frames = mine.until(lambda f: sum(x.get("event") == "run" for x in f) >= 2)
        rows = [f["data"] for f in mine_frames if f.get("event") == "run"]
        assert {r["id"] for r in rows} == {run["run_id"]}
        assert [r["status"] for r in rows] == ["running", "succeeded"]
        assert set(rows[0]) == {"id", "team_id", "project_id", "workflow", "status", "triggered_by", "started_at",
                                "finished_at", "cost_usd", "subject", "thread", "title", "verdict", "trigger",
                                "links", "outcome", "risk", "location", "host_cancel"}
        theirs_frames = theirs.until(lambda f: any(x.get("event") == "run" for x in f))
        assert [f["data"]["id"] for f in theirs_frames if f.get("event") == "run"] == [other_run["run_id"]]


def test_run_routes_hide_other_teams(live, world):
    run = register(live.client(), world["key"])
    outsider = live.client(world["users"]["outsider"])
    for path in ("", "/events", "/stream"):
        assert outsider.get(f"/api/runs/{run['run_id']}{path}").status_code == 404
        assert live.client().get(f"/api/runs/{run['run_id']}{path}").status_code == 401
    assert outsider.get("/api/runs/nope").status_code == 404
    assert live.client(world["users"]["viewer"]).get(f"/api/runs/{run['run_id']}").status_code == 200


def test_stream_caps(engine, keyring, clock, world):
    with LiveServer(engine, keyring, clock, socket=False, sse_per_user=1) as live:
        run = register(live.client(), world["key"])
        viewer = live.client(world["users"]["viewer"])
        with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
            first = SSE(raw)
            first.until(lambda f: True)
            assert viewer.get("/api/stream/runs").status_code == 429
        deadline = time.monotonic() + 5
        while live.hub._streams and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not live.hub._streams


def test_slow_subscriber_is_told_to_reconnect(live, world):
    from armature.server.live import run_key
    run = register(live.client(), world["key"])
    sub = live.call(_subscribe(live.hub, run_key(run["run_id"])))
    post(live.client(), run["token"], *[line("log", None, i=i) for i in range(600)])
    assert sub.overflowed and live.hub.broker.overflows >= 1


async def _subscribe(hub, key):
    return hub.broker.subscribe([key])


def test_branches_keep_a_step_running(live, world):
    http = live.client()
    run = register(http, world["key"])
    token = run["token"]
    post(http, token, line("step_start", "roles", branches=2), line("step_end", "roles", branch="a"))
    live.flush()

    def status():
        with live.engine.connect() as conn:
            return conn.execute(select(run_steps.c.status).where(run_steps.c.step_id == "roles")).scalar()

    assert status() == "running"
    post(http, token, line("step_failed", "roles", branch="b", reason="boom"))
    live.flush()
    assert status() == "failed"
    post(http, token, line("step_start", "fan", branches=["x", "y"]), line("step_end", "fan", branch="x"),
         line("step_failed", "fan", branch="y", fail_as_value=True))
    live.flush()
    with live.engine.connect() as conn:
        assert conn.execute(select(run_steps.c.status).where(run_steps.c.step_id == "fan")).scalar() == "succeeded"


def test_lost_sweep_and_revival(live, world, clock):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("log", None, text="hi"))
    live.flush()
    clock.advance(minutes=30)
    assert live.call(live.hub.sweep()) == []
    clock.advance(minutes=31)
    assert live.call(live.hub.sweep()) == [run["run_id"]]
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(f"/api/runs/{run['run_id']}").json()["run"]["status"] == "lost"
    assert viewer.get("/api/runs", params={"status": "lost"}).json()["runs"][0]["id"] == run["run_id"]
    post(http, run["token"], line("log", None, text="still here"))
    live.flush()
    with live.engine.connect() as conn:
        row = conn.execute(select(runs.c.status, runs.c.finished_at).where(runs.c.id == run["run_id"])).one()
    assert (row.status, row.finished_at) == ("running", None)


def test_rate_limit_marks_a_gap(live, world, monkeypatch):
    from armature.server import ingest
    monkeypatch.setattr(ingest, "RATE_BURST", 3.0)
    monkeypatch.setattr(ingest, "RATE_PER_S", 0.001)
    http = live.client()
    run = register(http, world["key"])
    result = post(http, run["token"], *[line("log", None, i=i) for i in range(5)])
    assert result == {"accepted": 3, "rejected": 2, "duplicates": 0}
    assert post(http, run["token"], line("run_end", None, status="ok"))["accepted"] == 1
    live.flush()
    kinds = [(r.kind, r.payload) for r in _stored(live.engine, run["run_id"])][-2:]
    assert kinds == [("dropped", {"count": 2, "reason": "server_limit"}), ("run_end", {"status": "ok"})]


def test_shutdown_flushes(engine, keyring, clock, world):
    with LiveServer(engine, keyring, clock, socket=False) as live:
        run = register(live.client(), world["key"])
        post(live.client(), run["token"], line("log", None, text="bye"))
    assert len(_stored(engine, run["run_id"])) == 1


def test_ingest_off_without_lifespan(engine, keyring, clock):
    from tests.server.app_support import make_client
    response = make_client(engine, keyring, clock).post("/api/events", content=b"{}")
    assert response.status_code == 503



def test_step_end_outputs_are_stored_with_fan_out_branches_as_a_list(live, world, engine):
    http = live.client()
    run = register(http, world["key"])
    result = post(http, run["token"],
                  line("step_start", "resolve"),
                  line("step_end", "resolve", output={"files": ["a.py", "b.py"]}),
                  line("step_start", "review", branch=0, branches=2),
                  line("step_start", "review", branch=1, branches=2),
                  line("step_end", "review", branch=1, branches=2, output={"finding": "b"}),
                  line("step_end", "review", branch=0, branches=2, output={"finding": "a"}),
                  line("step_start", "post"),
                  line("step_end", "post", output={"body": "cut"}, truncated=True),
                  line("step_start", "plain"),
                  line("step_end", "plain"))
    assert result["accepted"] == 10
    live.flush()
    with engine.connect() as conn:
        outputs = dict(conn.execute(select(run_steps.c.step_id, run_steps.c.output)
                                    .where(run_steps.c.run_id == run["run_id"])).all())
        without = conn.execute(select(run_steps.c.step_id).where(
            run_steps.c.run_id == run["run_id"], run_steps.c.output.is_(None))).scalars().all()
    assert outputs["resolve"] == {"files": ["a.py", "b.py"]}
    assert outputs["review"] == [{"finding": "a"}, {"finding": "b"}]
    assert outputs["post"] == {"body": "cut", "_truncated": True}
    assert without == ["plain"]


def test_registration_stores_subject_and_trigger_ref(live, world):
    http = live.client()
    ref = {"repo": "fs-eng/android-tree", "number": 123, "draft": False, "job": {"mode": "fix"}, "dry_run": True,
           "source": "spoofed", "arm_id": "an-arm-the-runner-made-up"}
    run = register(http, world["key"], subject="fs-eng/android-tree#123", trigger_ref=ref)
    with live.engine.connect() as conn:
        row = conn.execute(select(runs.c.subject, runs.c.trigger_ref).where(runs.c.id == run["run_id"])).one()
    assert row.subject == "fs-eng/android-tree#123"
    assert row.trigger_ref == {"repo": "fs-eng/android-tree", "number": 123, "draft": False, "job": {"mode": "fix"},
                               "source": "runner"}
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(f"/api/runs/{run['run_id']}").json()["run"]["dry_run"] is False
    auth = {"Authorization": f"Bearer {world['key']}"}
    bad = [{"subject": "has space"}, {"subject": "x" * 192}, {"subject": 7}, {"subject": "a;b"},
           {"trigger_ref": [1]}, {"trigger_ref": {"a": [1]}}, {"trigger_ref": {"a": None}},
           {"trigger_ref": {"a": "x" * 4100}}, {"trigger_ref": {"a": {"b": {"c": {"d": {"e": 1}}}}}}]
    for extra in bad:
        response = http.post("/api/runner/runs", json={"workflow": "w", **extra}, headers=auth)
        assert response.status_code == 400, extra
    assert http.post("/api/runner/runs", json={"workflow": "w", "subject": "MEMTEMDIS-886"},
                     headers=auth).status_code == 200


def test_summary_events_merge_into_the_run(live, world):
    http = live.client()
    run = register(http, world["key"], subject="fs-eng/android-tree#7")
    token = run["token"]
    result = post(http, token,
                  line("summary", None, title="Fix the crash", links=[
                      {"label": "Pull request", "url": "https://github.com/fs-eng/android-tree/pull/7"},
                      {"label": "Bad", "url": "javascript:alert(1)"},
                      {"label": "Plain", "url": "http://example.com"},
                      {"label": "Protocol relative", "url": "//evil.example.com/x"},
                      {"label": "Dashboard", "url": "/runs/abc"}],
                       fields={"platform": "android", "bad key": "x", "mode": "review"}),
                  line("summary", None, verdict="approve", fields={"mode": "fix"}, links=[
                      {"label": "PR", "url": "https://github.com/fs-eng/android-tree/pull/7"},
                      {"label": "Review", "url": "https://github.com/fs-eng/android-tree/pull/7#r1"}]),
                  line("summary", None, nothing="here"))
    assert result == {"accepted": 3, "rejected": 0, "duplicates": 0}
    expected = {"title": "Fix the crash", "verdict": "approve",
                "links": [{"label": "PR", "url": "https://github.com/fs-eng/android-tree/pull/7"},
                          {"label": "Dashboard", "url": "/runs/abc"},
                          {"label": "Review", "url": "https://github.com/fs-eng/android-tree/pull/7#r1"}],
                "fields": {"platform": "android", "mode": "fix"}}
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(f"/api/runs/{run['run_id']}").json()["run"]["summary"] == expected
    live.flush()
    with live.engine.connect() as conn:
        assert conn.execute(select(runs.c.summary).where(runs.c.id == run["run_id"])).scalar() == expected
    post(http, token, line("summary", None, title="Fix the crash, again"))
    live.flush()
    with live.engine.connect() as conn:
        stored = conn.execute(select(runs.c.summary).where(runs.c.id == run["run_id"])).scalar()
    assert stored == {**expected, "title": "Fix the crash, again"}


def test_outcome_reaches_the_run_and_its_list_row_but_a_runner_never_sets_risk(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("summary", None, outcome="recommended", risk="low"))
    post(http, run["token"], line("summary", None, outcome="pushed", fields={"risk": "low"}))
    live.flush()
    with live.engine.connect() as conn:
        stored = conn.execute(select(runs.c.outcome, runs.c.risk, runs.c.risk_reasons)
                              .where(runs.c.id == run["run_id"])).first()
    assert (stored.outcome, stored.risk, stored.risk_reasons) == ("pushed", None, None)
    viewer = live.client(world["users"]["viewer"])
    detail = viewer.get(f"/api/runs/{run['run_id']}").json()["run"]
    assert (detail["outcome"], detail["risk"]) == ("pushed", None)
    listed = viewer.get("/api/runs").json()["runs"]
    assert [(r["outcome"], r["risk"]) for r in listed] == [("pushed", None)]


@pytest.mark.parametrize(("column", "value"), [("head_sha", "a" * 40), ("review_round", 1),
                                               ("prev_round_run_id", "0" * 36), ("review_kind", "full")])
def test_a_runner_never_sets_a_runs_head_or_review_round(live, world, column, value):
    http = live.client()
    run = register(http, world["key"])
    for sent in ([line("summary", None, **{column: value}), line("summary", None, fields={column: value})],
                 [line("run_end", None, status="succeeded", **{column: value})]):
        post(http, run["token"], *sent)
        live.flush()
        with live.engine.connect() as conn:
            assert conn.execute(select(runs.c[column]).where(runs.c.id == run["run_id"])).scalar() is None
    detail = live.client(world["users"]["viewer"]).get(f"/api/runs/{run['run_id']}").json()["run"]
    assert detail[column] is None


def test_a_server_outcome_is_never_replaced_by_a_runner_summary(live, world):
    from armature.server.writer import server_outcome
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("summary", None, outcome="approved"))
    live.flush()
    with live.engine.begin() as conn:
        assert conn.execute(select(runs.c.outcome_source).where(runs.c.id == run["run_id"])).scalar() == "runner"
        server_outcome(conn, run["run_id"], "blocked")
    post(http, run["token"], line("summary", None, outcome="approved", title="Still trying"))
    live.flush()
    with live.engine.connect() as conn:
        stored = conn.execute(select(runs.c.outcome, runs.c.outcome_source, runs.c.summary)
                              .where(runs.c.id == run["run_id"])).first()
    assert (stored.outcome, stored.outcome_source) == ("blocked", "server")
    assert stored.summary == {"outcome": "approved", "title": "Still trying"}
    with pytest.raises(ValueError), live.engine.begin() as conn:
        server_outcome(conn, run["run_id"], "shipped")


def test_a_server_outcome_reaches_open_run_pages_and_the_live_summary(live, world):
    from armature.server.live import run_key
    from armature.server.writer import server_outcome
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("summary", None, title="Review"))
    live.flush()
    sub = live.call(_subscribe(live.hub, run_key(run["run_id"])))
    with live.engine.begin() as conn:
        server_outcome(conn, run["run_id"], "blocked")
        conn.execute(update(runs).where(runs.c.id == run["run_id"]).values(risk="high"))
    live.hub.run_changed(run["run_id"])
    frame = live.call(asyncio.wait_for(sub.queue.get(), 5)).frame
    assert '"outcome":"blocked"' in frame and '"risk":"high"' in frame
    post(http, run["token"], line("summary", None, outcome="approved"))
    live.flush()
    summary = live.hub.summary(run["run_id"])
    assert (summary["outcome"], summary["outcome_source"], summary["risk"]) == ("blocked", "server", "high")


def _wait_for_writes(live, run_id):
    deadline = time.monotonic() + 5
    while not live.hub.writer.pending(run_id, 0) and time.monotonic() < deadline:
        time.sleep(0.02)
    live.flush()


def test_server_findings_join_a_live_run_as_its_own_finding_events(live, world):
    from armature.server.db.schema import findings
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("log", None, text="hi"))
    live.flush()
    live.hub.server_findings(run["run_id"], "decisions", [{"claim": "kept", "decision": "block", "reason": "r"}])
    live.hub.server_findings(run["run_id"], "decisions", [{"claim": "later", "decision": "drop", "reason": "r"}],
                             "second")
    _wait_for_writes(live, run["run_id"])
    events = [(e.kind, e.step_id, (e.payload or {}).get("parent_step")) for e in _stored(live.engine, run["run_id"])]
    assert ("finding", "decisions", None) in events and ("finding", "decisions", "second") in events
    with live.engine.connect() as conn:
        rows = {r.body: r for r in conn.execute(select(findings).where(findings.c.run_id == run["run_id"]))}
    assert (rows["kept"].decision, rows["kept"].reason, rows["kept"].step_id) == ("block", "r", "decisions")
    assert rows["later"].step_id == "second/decisions"


def test_server_findings_for_a_run_that_left_the_hub_are_still_stored(live, world):
    from armature.server.db.schema import findings
    run = register(live.client(), world["key"])
    live.hub._loop.call_soon_threadsafe(live.hub._server_findings, run["run_id"], "decisions",
                                        [{"claim": "kept", "decision": "suggest", "reason": "r"}])
    deadline = time.monotonic() + 5
    found = None
    while found is None and time.monotonic() < deadline:
        with live.engine.connect() as conn:
            found = conn.execute(select(findings).where(findings.c.run_id == run["run_id"])).first()
        time.sleep(0.02)
    assert found is not None and (found.decision, found.step_id) == ("suggest", "decisions")


def test_server_findings_for_a_run_the_hub_never_held_carry_their_round(live, world):
    run = register(live.client(), world["key"])
    live.hub._runs.pop(run["run_id"], None)
    live.hub.server_findings(run["run_id"], "decisions", [{"claim": "kept", "decision": "block", "reason": "r"}],
                             "first")
    with live.engine.connect() as conn:
        assert conn.execute(select(findings.c.step_id).where(findings.c.run_id == run["run_id"])).scalar_one() == \
            "first/decisions"


def test_a_finding_without_a_key_is_keyed_as_github_post_review_keys_it_and_says_whether_it_blocks():
    from armature.server.ingest import finding_row
    now = datetime(2026, 11, 16, tzinfo=UTC)

    def row(**payload):
        return finding_row("p", "team_lead", now, {"path": "a.py", "line": 3, "body": "It leaks.", **payload})

    assert row(claim="the handle leaks")["finding_key"] == finding_key({"path": "a.py", "claim": "the handle leaks"})
    assert row()["finding_key"] == finding_key({"path": "a.py", "body": "It leaks."})
    message_only = finding_row("p", "team_lead", now, {"path": "a.py", "message": "It leaks."})
    assert message_only["finding_key"] == finding_key({"path": "a.py", "body": "It leaks."}) != finding_key(
        {"path": "a.py"})
    assert row(finding_key="0123456789ab", lineage_key="x")["finding_key"] == "0123456789ab"
    assert "lineage_key" not in row(lineage_key="x")
    assert [row(**p)["blocking"] for p in ({"decision": "block"}, {"decision": "suggest"}, {"decision": "drop"},
                                           {}, {"decision": "suggest", "blocking": True},
                                           {"decision": "block", "blocking": False}, {"blocking": "yes"})] == [
        True, False, None, None, True, False, None]


def test_findings_keep_the_leads_decision(live, world):
    http = live.client()
    run = register(http, world["key"])
    sha = "a" * 40
    post(http, run["token"],
         line("finding", "team_lead", path="a.py", body="kept", decision="fixed", raised_by=["security", 7, ""],
              fix_sha=sha, reason="r"),
         line("finding", "team_lead", path="b.py", body="old", disposition="drop_rebutted"),
         line("finding", "team_lead", path="c.py", body="bad", decision="maybe", fix_sha="HEAD"))
    live.flush()
    with live.engine.connect() as conn:
        rows = {r.path: r for r in conn.execute(select(findings).where(findings.c.run_id == run["run_id"]))}
    assert (rows["a.py"].decision, rows["a.py"].raised_by, rows["a.py"].fix_sha, rows["a.py"].reason) == \
        ("fixed", ["security"], sha, "r")
    assert rows["b.py"].decision == "drop"
    assert (rows["c.py"].decision, rows["c.py"].fix_sha, rows["c.py"].raised_by) == (None, None, None)


CACHE = "the cache is never cleared on sign-out; nothing calls signedOut()"


def _decisions_called(live, world, run_id):
    with live.engine.begin() as conn:
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run_id, team_id=world["team"], step_id="decisions",
                                               tool="review.decisions", mode="live", outcome="ok",
                                               created_at=datetime.now(UTC)))


def test_candidates_are_stored_once_never_as_decisions_and_match_the_leads(live, world, clock):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"],
         line("candidate", "correctness", parent_step="first", ordinal=0, specialist="correctness", path="M.kt",
              line=7, severity="high", blocking=True, claim=CACHE, body="b", suggestion="s"),
         line("candidate", "correctness", parent_step="first", ordinal=1, specialist="correctness", path="N.kt",
              line=3, claim="rename the helper"),
         line("candidate", "security", parent_step="first", ordinal=0, specialist="security", path="M.kt", line=9,
              claim="one user's memories can show to the next user on a shared device."),
         line("candidate", "test_adequacy", parent_step="first", ordinal=0, specialist="test_adequacy",
              path="M.kt", line=9, claim="signedOut() is untested"),
         line("candidate", "correctness", parent_step="first", ordinal=MAX_CANDIDATES, claim="over the cap"),
         line("candidate", "correctness", parent_step="first", ordinal=True, claim="not an ordinal"),
         line("candidate", None, ordinal=0, claim="no step"))
    live.flush()
    with live.engine.connect() as conn:
        rows = {(r.step_key, r.ordinal): r for r in conn.execute(
            select(finding_candidates).where(finding_candidates.c.run_id == run["run_id"]))}
        assert conn.execute(select(findings).where(findings.c.run_id == run["run_id"])).first() is None
    assert sorted(rows) == [("first/correctness", 0), ("first/correctness", 1), ("first/security", 0),
                            ("first/test_adequacy", 0)]
    cache = rows[("first/correctness", 0)]
    assert (cache.team_id, cache.specialist, cache.path, cache.line, cache.severity, cache.blocking, cache.body,
            cache.suggestion) == (world["team"], "correctness", "M.kt", 7, "high", True, "b", "s")
    assert cache.finding_key == finding_key({"path": "M.kt", "claim": CACHE})

    _decisions_called(live, world, run["run_id"])
    live.hub.server_findings(run["run_id"], "decisions", [
        {"claim": "The cache is never cleared on sign-out, so the next user sees the previous user's memories.",
         "path": "M.kt", "raised_by": ["correctness", "security"], "decision": "block", "reason": "r"},
        {"claim": "signedOut() is untested", "path": "M.kt", "raised_by": ["team_lead"], "decision": "suggest",
         "reason": "r", "finding_key": finding_key({"path": "M.kt", "claim": "signedOut() is untested"})}], "first")
    _wait_for_writes(live, run["run_id"])
    with live.engine.connect() as conn:
        decided = {r.body[:3]: r.id for r in conn.execute(select(findings).where(findings.c.run_id == run["run_id"]))}
        matched = {(r.step_key, r.ordinal): (r.method, r.finding_id, r.match_ordinal) for r in conn.execute(
            select(finding_candidates.c.step_key, finding_candidates.c.ordinal, candidate_matches.c.method,
                   candidate_matches.c.finding_id, candidate_matches.c.match_ordinal)
            .join(candidate_matches, candidate_matches.c.candidate_id == finding_candidates.c.id)
            .where(finding_candidates.c.run_id == run["run_id"]))}
    assert matched == {("first/correctness", 0): ("heuristic", decided["The"], 0),
                       ("first/security", 0): ("heuristic", decided["The"], 0),
                       ("first/test_adequacy", 0): ("key", decided["sig"], 0),
                       ("first/correctness", 1): ("none", None, 0)}

    replay = RunChange(candidates=[
        candidate_row(world["team"], None, "first/correctness", "correctness", clock(), {"ordinal": o, "claim": "x"})
        for o in (0, 5)])
    with live.engine.begin() as conn:
        _apply_run(conn, run["run_id"], replay)
    with live.engine.connect() as conn:
        stored = {r.ordinal: r.claim for r in conn.execute(select(finding_candidates).where(
            finding_candidates.c.run_id == run["run_id"], finding_candidates.c.step_key == "first/correctness"))}
    assert stored == {0: CACHE, 1: "rename the helper", 5: "x"}


def test_candidates_arriving_after_the_decisions_match_them_without_a_backfill(live, world):
    http = live.client()
    run = register(http, world["key"])
    _decisions_called(live, world, run["run_id"])
    live.hub.server_findings(run["run_id"], "decisions", [
        {"claim": "signedOut() is untested", "path": "M.kt", "raised_by": ["test_adequacy"], "decision": "suggest",
         "reason": "r", "finding_key": finding_key({"path": "M.kt", "claim": "signedOut() is untested"})}])
    _wait_for_writes(live, run["run_id"])
    post(http, run["token"],
         line("candidate", "test_adequacy", ordinal=0, specialist="test_adequacy", path="M.kt", line=9,
              claim="signedOut() is untested"),
         line("candidate", "test_adequacy", ordinal=1, specialist="test_adequacy", path="N.kt", claim="other"))
    live.flush()
    with live.engine.connect() as conn:
        decided = conn.execute(select(findings.c.id).where(findings.c.run_id == run["run_id"])).scalar_one()
        matched = sorted((r.ordinal, r.method, r.finding_id) for r in conn.execute(
            select(finding_candidates.c.ordinal, candidate_matches.c.method, candidate_matches.c.finding_id)
            .join(candidate_matches, candidate_matches.c.candidate_id == finding_candidates.c.id)
            .where(finding_candidates.c.run_id == run["run_id"])))
    assert matched == [(0, "key", decided), (1, "none", None)]


def test_each_fan_out_branch_keeps_its_own_candidates(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"],
         line("candidate", "review", ordinal=0, branch=0, branches=2, specialist="correctness", claim="a"),
         line("candidate", "review", ordinal=0, branch=1, branches=2, specialist="security", claim="b"),
         line("candidate", "review", ordinal=0, branch=1, branches=2, specialist="security", claim="retried"))
    live.flush()
    with live.engine.connect() as conn:
        rows = sorted((r.step_key, r.ordinal, r.specialist, r.claim) for r in conn.execute(
            select(finding_candidates).where(finding_candidates.c.run_id == run["run_id"])))
    assert rows == [("review#0", 0, "correctness", "a"), ("review#1", 0, "security", "b")]


def test_candidate_row_caps_hostile_payloads():
    now = datetime(2026, 11, 13, tzinfo=UTC)
    row = candidate_row("team", "project", "s" * 191, "step", now,
                        {"ordinal": 3, "specialist": "x\x00" * 50, "path": "p\x00", "line": 2 ** 40, "blocking": "yes",
                         "claim": "c" * 5000, "body": "b" * 70_000, "severity": "high" * 9})
    assert (row["specialist"], row["path"], row["line"], row["blocking"]) == ("x" * 50, "p", None, None)
    assert (len(row["claim"]), len(row["body"]), len(row["severity"])) == (2000, 60_000, 16)
    assert candidate_row("team", None, "k", "step", now, {"ordinal": 0, "line": "7"})["line"] is None
    assert candidate_row("team", None, "k", "step", now, {"ordinal": 0})["specialist"] == "step"
    for bad in (-1, MAX_CANDIDATES, True, "0", None):
        assert candidate_row("team", None, "k", "step", now, {"ordinal": bad}) is None


def test_summary_is_redacted(live, world, keyring):
    with live.engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=world["team"], name="GH_TOKEN", value="hunter2-secret",
                                actor_id=None)
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("summary", None, title="token hunter2-secret leaked",
                                  fields={"note": "hunter2-secret"}))
    live.flush()
    with live.engine.connect() as conn:
        stored = conn.execute(select(runs.c.summary).where(runs.c.id == run["run_id"])).scalar()
    assert "hunter2-secret" not in str(stored) and stored["title"].startswith("token ")


def test_run_list_filters_by_subject_and_shows_the_summary(live, world):
    http = live.client()
    first = register(http, world["key"], subject="fs-eng/android-tree#7")
    register(http, world["key"], subject="MEMTEMDIS-886")
    register(http, world["key"])
    post(http, first["token"], line("summary", None, title="Fix it", verdict="request_changes"))
    live.flush()
    viewer = live.client(world["users"]["viewer"])
    listed = viewer.get("/api/runs", params={"subject": "fs-eng/android-tree#7"}).json()["runs"]
    assert [(r["id"], r["subject"], r["title"], r["verdict"]) for r in listed] == \
        [(first["run_id"], "fs-eng/android-tree#7", "Fix it", "request_changes")]
    everything = viewer.get("/api/runs").json()["runs"]
    assert len(everything) == 3 and {r["subject"] for r in everything} == {"fs-eng/android-tree#7", "MEMTEMDIS-886",
                                                                             None}
    assert viewer.get("/api/runs", params={"subject": "x" * 192}).status_code == 400


def test_pull_request_runs_join_the_issue_thread_by_title_or_link(live, world):
    http = live.client()
    dev = register(http, world["key"], workflow="development", subject="MEM-5")
    review = register(http, world["key"], workflow="code-review", subject="acme/app#9")
    titled = register(http, world["key"], workflow="code-review", subject="acme/app#10")
    elsewhere = register(http, world["other_key"], workflow="code-review", subject="acme/app#9")
    post(http, titled["token"], line("summary", None, title="[MEM-6] Tidy"))
    post(http, review["token"], line("summary", None, title="No prefix here"))
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(f"/api/runs/{titled['run_id']}").json()["run"]["thread"] == "MEM-6"
    post(http, dev["token"], line("summary", None, links=[
        {"label": "Pull request", "url": "https://github.com/acme/app/pull/9"}]))
    assert viewer.get(f"/api/runs/{review['run_id']}").json()["run"]["thread"] == "MEM-5"
    live.flush()
    fix = register(http, world["key"], workflow="pr-fix", subject="acme/app#9")
    with live.engine.connect() as conn:
        threads = dict(conn.execute(select(runs.c.id, runs.c.thread)).all())
    assert {name: threads[run["run_id"]] for name, run in
            (("dev", dev), ("review", review), ("titled", titled), ("elsewhere", elsewhere), ("fix", fix))} == \
        {"dev": "MEM-5", "review": "MEM-5", "titled": "MEM-6", "elsewhere": None, "fix": "MEM-5"}
    grouped = viewer.get("/api/runs", params={"group": True}).json()["runs"]
    assert {(r["thread"], r["group_size"]) for r in grouped} == {("MEM-5", 3), ("MEM-6", 1)}


def test_a_pull_request_opened_after_its_issue_run_linked_it_joins_that_thread(live, world):
    http = live.client()
    dev = register(http, world["key"], workflow="development", subject="MEM-7")
    post(http, dev["token"], line("summary", None, links=[
        {"label": "Pull request", "url": "https://github.com/acme/app/pull/12"}]))
    live.flush()
    review = register(http, world["key"], workflow="code-review", subject="acme/app#12")
    near_miss = register(http, world["key"], workflow="code-review", subject="acme/app#1")
    longer = register(http, world["key"], workflow="code-review", subject="acme/app#123")
    elsewhere = register(http, world["other_key"], workflow="code-review", subject="acme/app#12")
    with live.engine.connect() as conn:
        threads = dict(conn.execute(select(runs.c.id, runs.c.thread)).all())
    assert [threads[run["run_id"]] for run in (review, near_miss, longer, elsewhere)] == ["MEM-7", None, None, None]


def test_a_titled_run_brings_its_pull_requests_other_runs_into_the_thread(live, world):
    http = live.client()
    first = register(http, world["key"], workflow="code-review", subject="acme/app#20")
    second = register(http, world["key"], workflow="pr-fix", subject="acme/app#20")
    post(http, first["token"], line("step_start", "x"))
    post(http, second["token"], line("summary", None, title="[MEM-8] Tidy"))
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(f"/api/runs/{first['run_id']}").json()["run"]["thread"] == "MEM-8"
    live.flush()
    with live.engine.connect() as conn:
        threads = dict(conn.execute(select(runs.c.id, runs.c.thread)).all())
    assert (threads[first["run_id"]], threads[second["run_id"]]) == ("MEM-8", "MEM-8")


def test_run_list_filters_by_workflows_and_hides_hidden_ones(live, world, monkeypatch):
    from armature.server import display
    http = live.client()
    for workflow in ("code-review", "digest", "jira-work"):
        register(http, world["key"], workflow=workflow)
    viewer = live.client(world["users"]["viewer"])

    def names(**params):
        return sorted(r["workflow"] for r in viewer.get("/api/runs", params=params).json()["runs"])

    assert names() == ["code-review", "digest", "jira-work"]
    assert names(workflows="code-review,jira-work") == ["code-review", "jira-work"]
    assert names(workflows=" digest ,,") == ["digest"]
    real = display.load
    monkeypatch.setattr(display, "load", lambda conn: {**real(conn), "workflow_labels": {
        "digest": {"label": "", "color": "", "hidden": True}, "jira-work": {"label": "Jira", "color": ""}}})
    assert names() == ["code-review", "jira-work"]
    assert names(include_hidden="1") == ["code-review", "digest", "jira-work"]
    assert names(workflows="digest") == ["digest"]
    assert names(workflow="digest") == ["digest"]


def test_ops_events_locate_a_running_run_and_a_stated_location_wins(live, world):
    http = live.client()
    viewer = live.client(world["users"]["viewer"])
    run = register(http, world["key"], location={"kind": "local", "chip": "Apple M1", "machine": "arm64"})

    def where():
        return viewer.get(f"/api/runs/{run['run_id']}").json()["run"]["location"]

    assert where()["label"] == "M1 (local)"
    post(http, run["token"], line("log", "validate", message="building fs-eng/android-tree#7 on aws-mac: ./gradlew"))
    assert where() == {"kind": "aws-mac", "label": "AWS macOS"}
    post(http, run["token"], line("step_end", "implement", output={"placement": {"where": "m1"}}),
         line("step_end", "refine", output={"placement": {"where": "aws-session", "instance": "i-0c35"}}))
    assert where()["kind"] == "aws-mac"
    post(http, run["token"], line("summary", None, location={"kind": "aws-linux", "instance": "i-0c35"}))
    assert where() == {"kind": "aws-linux", "label": "AWS Linux", "instance": "i-0c35"}
    live.flush()
    listed = viewer.get("/api/runs").json()["runs"]
    assert [r["location"]["label"] for r in listed if r["id"] == run["run_id"]] == ["AWS Linux"]

    other = register(http, world["key"])
    post(http, other["token"],
         line("step_end", "implement", output={"placement": {"where": "aws-session", "launched": False}}),
         line("step_end", "build", output={"placement": {"where": "aws-session", "instance": "i-0abc",
                                                         "instance_type": "m7i.large", "market": "spot"}}))
    live.flush()
    with live.engine.connect() as conn:
        stored = dict(conn.execute(select(runs.c.id, runs.c.location)
                                   .where(runs.c.id.in_([run["run_id"], other["run_id"]]))).all())
    assert stored[run["run_id"]] == {"kind": "aws-linux", "label": "AWS Linux", "instance": "i-0c35"}
    assert stored[other["run_id"]] == {"kind": "aws-linux", "label": "AWS Linux", "instance": "i-0abc",
                                       "instance_type": "m7i.large"}
