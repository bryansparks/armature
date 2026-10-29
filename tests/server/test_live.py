"""Runner keys, registration, event ingest, the writer, and the live streams, on a real server."""
import os
import stat
import threading
import time

import pytest
from sqlalchemy import func, insert, select, update

from armature.events.emitter import Emitter
from armature.server import runners
from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    findings,
    memberships,
    projects,
    run_events,
    run_steps,
    run_tokens,
    runs,
    teams,
    workflows,
)
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
                            "cancelled_by", "error", "subject", "summary", "last_seq", "dry_run", "kind", "trigger"}
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
                                "finished_at", "cost_usd", "subject", "title", "verdict", "trigger"}
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
           "source": "spoofed"}
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
