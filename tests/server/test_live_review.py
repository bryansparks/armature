"""Ingest robustness, dedupe, fan-out, security, and stream lifecycle fixes, on a real server."""
import json
import socket
import time

import pytest
from sqlalchemy import delete, insert, select, update

from armature.server import ingest, runners
from armature.server.app import routes_live
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    findings,
    projects,
    run_events,
    run_steps,
    run_tokens,
    runs,
    sessions,
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
    other, _ = add_team(engine, clock, "web")
    users = {"lead": add_user(engine, keyring, clock, "lead")[0],
             "viewer": add_user(engine, keyring, clock, "viewer")[0],
             "outsider": add_user(engine, keyring, clock, "outsider")[0]}
    add_membership(engine, clock, users["lead"], "team", team, "team_lead")
    add_membership(engine, clock, users["viewer"], "team", team, "viewer")
    add_membership(engine, clock, users["outsider"], "team", other, "viewer")
    return {"team": team, "project": project, "other": other, "users": users,
            "key": add_runner_key(engine, keyring, clock, team)}


@pytest.fixture
def live(engine, keyring, clock):
    with LiveServer(engine, keyring, clock) as server:
        yield server


def _stored(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(run_events).where(run_events.c.run_id == run_id)
                            .order_by(run_events.c.seq)).all()


def _steps(engine, run_id):
    with engine.connect() as conn:
        return {r.step_key: r for r in conn.execute(select(run_steps).where(run_steps.c.run_id == run_id))}


def _raw(kind, **fields):
    return json.dumps({"v": 1, "kind": kind, "step_id": None, "payload": {}, **fields}).encode()


def test_bad_events_never_stop_good_ones(live, world, clock, monkeypatch):
    http = live.client()
    run = register(http, world["key"])
    deep = b'{"v": 1, "kind": "log", "payload": {"a": ' + b"[" * 5000 + b"]" * 5000 + b"}}"
    nested = {"x": {}}
    cursor = nested["x"]
    for _ in range(40):
        cursor["x"] = {}
        cursor = cursor["x"]
    result = post(http, run["token"], deep, _raw("log", payload=nested), _raw("log", ts="0001-01-01T00:00:00+00:00"),
                  _raw("log", ts="9999-12-31T23:59:59+00:00"), _raw("log", ts=["not", "a", "time"]),
                  _raw("log", ts="2026-10-23T11:00:00+00:00"))
    assert result == {"accepted": 4, "rejected": 2, "duplicates": 0}
    live.flush()
    stored = _stored(live.engine, run["run_id"])
    assert [r.ts for r in stored] == [clock()] * 3 + [clock().replace(hour=11)]
    assert [r.seq for r in stored] == [1, 2, 3, 4]

    calls = {"n": 0}
    real = runners.lookup_token

    def flaky(conn, digest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database down")
        return real(conn, digest)

    fresh = register(http, world["key"])
    monkeypatch.setattr(runners, "lookup_token", flaky)
    assert post(http, fresh["token"], line("log", None, text="lost"))["rejected"] == 1
    assert post(http, fresh["token"], line("log", None, text="kept"))["accepted"] == 1

    emitter_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    emitter_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
    emitter_socket.sendto(deep, live.hub.socket_path)
    emitter_socket.sendto(line("log", None, event_token=fresh["token"], text="after"), live.hub.socket_path)
    emitter_socket.close()
    deadline = time.monotonic() + 5
    while len(live.call(live.hub.events_after(fresh["run_id"], 0, 10))) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [e["payload"]["text"] for e in live.call(live.hub.events_after(fresh["run_id"], 0, 10))] == \
        ["kept", "after"]


def test_unloadable_secrets_refuse_the_run(live, world, monkeypatch):
    http = live.client()
    run = register(http, world["key"])
    real = ingest.team_secret_values

    def broken(*args):
        raise ValueError("a secret won't open")

    monkeypatch.setattr(ingest, "team_secret_values", broken)
    assert post(http, run["token"], line("log", None, text="x"))["rejected"] == 1
    monkeypatch.setattr(ingest, "team_secret_values", real)
    assert post(http, run["token"], line("log", None, text="x"))["accepted"] == 1


@pytest.mark.parametrize("fan_out,items", [(1, 3), (3, 9)])
def test_narrow_fan_out_keeps_one_tracker(live, world, fan_out, items, monkeypatch):
    http = live.client()
    run = register(http, world["key"])
    names = [f"b{i}" for i in range(items)]
    for wave in range(0, items, fan_out):
        post(http, run["token"], *[line("step_start", "roles", branches=items, branch=b)
                                   for b in names[wave:wave + fan_out]])
        live.flush()
        assert _steps(live.engine, run["run_id"])["roles"].status == "running"
        monkeypatch.setattr(ingest, "IDLE_STATE_S", -1.0)
        live.call(live.hub.sweep())
        assert run["run_id"] in live.hub._runs
        monkeypatch.setattr(ingest, "IDLE_STATE_S", 600.0)
        post(http, run["token"], *[line("step_end", "roles", branch=b) for b in names[wave:wave + fan_out]])
        live.flush()
        expected = "succeeded" if wave + fan_out >= items else "running"
        assert _steps(live.engine, run["run_id"])["roles"].status == expected


def test_reposted_batch_is_deduplicated(live, world):
    http = live.client()
    run = register(http, world["key"])
    batch = [_raw("log", eid=f"nonce1:{i}", payload={"i": i}) for i in range(1, 4)]
    assert post(http, run["token"], *batch) == {"accepted": 3, "rejected": 0, "duplicates": 0}
    assert post(http, run["token"], *batch, _raw("log", eid="nonce1:4")) == \
        {"accepted": 1, "rejected": 0, "duplicates": 3}
    assert post(http, run["token"], _raw("log", eid="nonce2:1"), _raw("log", eid="bad eid"))["accepted"] == 2
    live.flush()
    assert [r.seq for r in _stored(live.engine, run["run_id"])] == list(range(1, 7))


def test_lone_surrogate_is_replaced(live, world):
    http = live.client()
    run = register(http, world["key"])
    raw = b'{"v": 1, "kind": "tool_result", "step_id": "s\\ud800", "payload": {"content": "a\\udc00b", "k\\ud801": 1}}'
    assert post(http, run["token"], raw)["accepted"] == 1
    live.flush()
    row = _stored(live.engine, run["run_id"])[0]
    assert row.step_id == "s�" and row.payload == {"content": "a�b", "k�": 1}


def test_one_bad_row_never_drops_the_others(live, world):
    http = live.client()
    run = register(http, world["key"])
    with live.engine.begin() as conn:
        conn.execute(insert(run_events).values(run_id=run["run_id"], seq=2, ts=live.clock(), kind="log",
                                               payload={"planted": True}))
    live.hub._runs.pop(run["run_id"], None)
    with live.engine.begin() as conn:
        conn.execute(delete(run_events).where(run_events.c.run_id == run["run_id"]))
    post(http, run["token"], line("log", None, i=1))
    with live.engine.begin() as conn:
        conn.execute(insert(run_events).values(run_id=run["run_id"], seq=2, ts=live.clock(), kind="log",
                                               payload={"planted": True}))
    post(http, run["token"], line("log", None, i=2), line("log", None, i=3), line("cost", None, cost_usd=1.0))
    live.flush()
    stored = _stored(live.engine, run["run_id"])
    assert [(r.seq, r.payload.get("i", r.payload.get("planted"))) for r in stored][:3] == \
        [(1, 1), (2, True), (3, 3)]
    assert live.hub.writer.dropped == 1
    with live.engine.connect() as conn:
        assert float(conn.execute(select(runs.c.cost_usd).where(runs.c.id == run["run_id"])).scalar()) == 1.0


def test_post_limits(live, world, monkeypatch):
    http = live.client()
    run = register(http, world["key"])
    tokens = [register(http, world["key"])["token"] for _ in range(4)]
    lines = [line("log", None, event_token=t) for t in tokens]
    assert http.post("/api/events", content=b"\n".join(lines),
                     headers={"Authorization": f"Bearer {run['token']}"}).status_code == 400
    calls = {"n": 0}
    real = runners.lookup_token

    def counted(conn, digest):
        calls["n"] += 1
        return real(conn, digest)

    monkeypatch.setattr(runners, "lookup_token", counted)
    for _ in range(3):
        post(http, "y" * 43, line("log", None))
    assert calls["n"] == 1
    monkeypatch.setattr(live.hub, "ip_limit", ingest.RateLimit(0.001, 2))
    statuses = [http.post("/api/events", content=line("log", None),
                          headers={"Authorization": f"Bearer {run['token']}"}).status_code for _ in range(3)]
    assert statuses == [202, 202, 429]


def test_lru_caches_forget_the_oldest():
    cache = ingest.OrderedDict()
    for key in "abc":
        ingest._remember(cache, key, 1, 2)
    assert list(cache) == ["b", "c"]


def test_revoking_a_key_revokes_its_runs(live, world):
    lead = live.client(world["users"]["lead"])
    created = lead.post(f"/api/teams/{world['team']}/runner-keys", json={"name": "k"}, headers=live.csrf(lead)).json()
    http = live.client()
    run = register(http, created["key"])
    other = register(http, world["key"])
    assert post(http, run["token"], line("log", None))["accepted"] == 1
    lead.post(f"/api/teams/{world['team']}/runner-keys/{created['runner_key']['id']}/delete",
              headers=live.csrf(lead))
    with live.engine.connect() as conn:
        assert conn.execute(select(run_tokens.c.run_id)).scalars().all() == [other["run_id"]]
    assert post(http, run["token"], line("log", None))["rejected"] == 1
    assert post(http, other["token"], line("log", None))["accepted"] == 1


def test_runner_keys_tokens_and_step_ids_are_redacted(live, world, keyring):
    from armature.server import secrets as secret_store
    with live.engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=world["team"], name="S", value="sekrit-value", actor_id=None)
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("log", "step sekrit-value", key=world["key"], mine=f"t={run['token']}"))
    live.flush()
    row = _stored(live.engine, run["run_id"])[0]
    assert row.step_id == "step [redacted]"
    assert row.payload == {"key": "[redacted]", "mine": "t=[redacted]"}


def _stream_frames(live, client, url, done, timeout=10):
    with client.stream("GET", url) as raw:
        frames = SSE(raw)
        seen = []
        deadline = time.monotonic() + timeout
        for frame in frames:
            seen.append(frame)
            if done(seen) or time.monotonic() > deadline:
                break
        else:
            seen.append({"closed": True})
        return seen


def test_stream_ends_when_the_session_does(live, world, monkeypatch):
    monkeypatch.setattr(routes_live, "HEARTBEAT_S", 0.2)
    run = register(live.client(), world["key"])
    viewer = live.client(world["users"]["viewer"])
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        frames = SSE(raw)
        frames.until(lambda f: True)
        with live.engine.begin() as conn:
            conn.execute(delete(sessions).where(sessions.c.user_id == world["users"]["viewer"]))
        started = time.monotonic()
        rest = list(frames)
        assert time.monotonic() - started < 5
        assert not any(f.get("event") == "run-event" for f in rest)


def test_stream_loses_access_when_membership_goes(live, world, monkeypatch):
    monkeypatch.setattr(routes_live, "HEARTBEAT_S", 0.2)
    run = register(live.client(), world["key"])
    viewer = live.client(world["users"]["viewer"])
    from armature.server.db.schema import memberships
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        frames = SSE(raw)
        frames.until(lambda f: True)
        with live.engine.begin() as conn:
            conn.execute(delete(memberships).where(memberships.c.user_id == world["users"]["viewer"]))
        started = time.monotonic()
        list(frames)
        assert time.monotonic() - started < 5


def test_stream_lifetime_and_shutdown(live, world, monkeypatch):
    monkeypatch.setattr(routes_live, "STREAM_LIFETIME_S", 0.5)
    run = register(live.client(), world["key"])
    viewer = live.client(world["users"]["viewer"])
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        assert any(f.get("event") == "reconnect" for f in SSE(raw))
    monkeypatch.setattr(routes_live, "STREAM_LIFETIME_S", 600.0)
    monkeypatch.setattr(routes_live, "HEARTBEAT_S", 0.2)
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        frames = SSE(raw)
        frames.until(lambda f: True)
        live.hub.closing = True
        started = time.monotonic()
        list(frames)
        assert time.monotonic() - started < 5
    live.hub.closing = False


def test_full_replay_asks_to_reconnect(live, world, monkeypatch):
    monkeypatch.setattr(routes_live, "REPLAY_LIMIT", 3)
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], *[line("log", None, i=i) for i in range(5)])
    viewer = live.client(world["users"]["viewer"])
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        frames = list(SSE(raw))
    assert [int(f["id"]) for f in frames if f.get("event") == "run-event"] == [1, 2, 3]
    assert frames[-1]["event"] == "reconnect"
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream", headers={"Last-Event-ID": "3"}) as raw:
        frames = SSE(raw).until(lambda f: sum(x.get("event") == "run-event" for x in f) >= 2)
    assert [int(f["id"]) for f in frames if f.get("event") == "run-event"] == [4, 5]


def test_sweep_spares_a_run_that_just_spoke(live, world, clock, monkeypatch):
    assert live.hub.lost_after.total_seconds() == 30 * 60
    http = live.client()
    run = register(http, world["key"])
    clock.advance(minutes=31)
    post(http, run["token"], line("progress", None))

    async def no_flush():
        return None

    monkeypatch.setattr(live.hub.writer, "flush", no_flush)
    assert live.call(live.hub.sweep()) == [run["run_id"]]
    assert live.hub.summary(run["run_id"])["status"] == "running"
    monkeypatch.undo()
    live.flush()
    with live.engine.connect() as conn:
        assert conn.execute(select(runs.c.status).where(runs.c.id == run["run_id"])).scalar() == "running"


def test_child_steps_never_merge_into_the_parent(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("step_start", "review"), line("step_start", "review", parent_step="lead"),
         line("cost", "review", parent_step="lead", cost_usd=0.5), line("step_end", "review", parent_step="lead"),
         line("cost", "review", cost_usd=0.25))
    live.flush()
    steps = _steps(live.engine, run["run_id"])
    assert set(steps) == {"review", "lead/review"}
    assert steps["review"].status == "running" and float(steps["review"].cost_usd) == 0.25
    assert steps["lead/review"].status == "succeeded" and steps["lead/review"].parent_step == "lead"
    shown = live.client(world["users"]["viewer"]).get(f"/api/runs/{run['run_id']}").json()["steps"]
    assert {(s["step_key"], s["step_id"], s["parent_step"]) for s in shown} == \
        {("review", "review", None), ("lead/review", "review", "lead")}


def test_findings_route(live, world, clock):
    http = live.client()
    run = register(http, world["key"])
    for index in range(3):
        clock.advance(seconds=1)
        post(http, run["token"], line("finding", "security", path=f"f{index}.py", line=index, severity="high",
                                      title=f"t{index}", body="b"))
    live.flush()
    viewer = live.client(world["users"]["viewer"])
    first = viewer.get(f"/api/runs/{run['run_id']}/findings", params={"limit": 2}).json()
    assert [f["path"] for f in first["findings"]] == ["f2.py", "f1.py"]
    assert set(first["findings"][0]) == {"id", "path", "line", "severity", "title", "body", "step_id", "created_at",
                                         "raised_by", "decision", "reason", "fix_sha"}
    assert first["findings"][0]["title"] == "t2" and first["findings"][0]["step_id"] == "security"
    rest = viewer.get(f"/api/runs/{run['run_id']}/findings",
                      params={"limit": 2, "before": first["next_before"]}).json()
    assert [f["path"] for f in rest["findings"]] == ["f0.py"] and rest["next_before"] is None
    outsider = live.client(world["users"]["outsider"])
    assert outsider.get(f"/api/runs/{run['run_id']}/findings").status_code == 404
    with live.engine.connect() as conn:
        assert len(conn.execute(select(findings)).all()) == 3


def test_registration_resolves_workflow_scope_and_project_repo(live, world, clock):
    team_wf, instance_wf, only_instance = new_id(), new_id(), new_id()
    with live.engine.begin() as conn:
        conn.execute(insert(workflows).values(id=instance_wf, team_id=None, scope=INSTANCE_SCOPE, name="review",
                                              current_version=1, created_at=clock(), updated_at=clock()))
        conn.execute(insert(workflows).values(id=team_wf, team_id=world["team"], scope=world["team"], name="review",
                                              current_version=1, created_at=clock(), updated_at=clock()))
        conn.execute(insert(workflows).values(id=only_instance, team_id=None, scope=INSTANCE_SCOPE, name="digest",
                                              current_version=1, created_at=clock(), updated_at=clock()))
        conn.execute(update(projects).where(projects.c.id == world["project"]).values(repo="fs-eng/mobile-app"))
    http = live.client()
    by_team = register(http, world["key"], workflow="review", project="fs-eng/mobile-app")
    by_instance = register(http, world["key"], workflow="digest", project="mobile-app")
    unknown = register(http, world["key"], workflow="adhoc")
    with live.engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(runs.c.id, runs.c.workflow_id, runs.c.project_id))}
    assert (rows[by_team["run_id"]].workflow_id, rows[by_team["run_id"]].project_id) == (team_wf, world["project"])
    assert (rows[by_instance["run_id"]].workflow_id, rows[by_instance["run_id"]].project_id) == \
        (only_instance, world["project"])
    assert rows[unknown["run_id"]].workflow_id is None
