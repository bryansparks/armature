"""Run artifacts: sent in parts by the emitter, redacted, stored by name, and read back by viewers."""
import json
import time

import pytest
from sqlalchemy import select, update

from armature.events.emitter import ARTIFACT_PART_BYTES, MAX_ARTIFACT_BYTES, Emitter
from armature.server import secrets as secret_store
from armature.server.app.routes_live import OUTPUT_SHOWN_BYTES
from armature.server.db.schema import run_artifacts, run_events, run_steps
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
    team, _ = add_team(engine, clock, "mobile")
    other, _ = add_team(engine, clock, "web")
    users = {"viewer": add_user(engine, keyring, clock, "viewer")[0],
             "outsider": add_user(engine, keyring, clock, "outsider")[0]}
    add_membership(engine, clock, users["viewer"], "team", team, "viewer")
    add_membership(engine, clock, users["outsider"], "team", other, "viewer")
    return {"team": team, "users": users, "key": add_runner_key(engine, keyring, clock, team)}


@pytest.fixture
def live(engine, keyring, clock):
    with LiveServer(engine, keyring, clock) as server:
        yield server


def _rows(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(run_artifacts).where(run_artifacts.c.run_id == run_id)).all()


def _stored(live, run_id, timeout: float = 5.0):
    """Rows once the hub has read every datagram, which arrive after the sender returns."""
    deadline = time.monotonic() + timeout
    while True:
        live.flush()
        rows = _rows(live.engine, run_id)
        if rows or time.monotonic() > deadline:
            return rows
        time.sleep(0.05)


def _send(emitter: Emitter, *args, **kwargs) -> None:
    assert emitter.artifact(*args, **kwargs) is True
    assert emitter.flush(10)
    emitter.close()


def _review(size: int) -> str:
    lines, total = ["# Code review", ""], 0
    while total < size:
        row = f"| security | `app/Auth.swift:{total}` | block | The token is logged before the request is signed. |"
        lines.append(row)
        total += len(row) + 1
    return "\n".join(lines)


@pytest.mark.parametrize("transport", ["http", "socket"])
def test_a_large_artifact_arrives_in_parts_and_reads_back(live, world, transport):
    http = live.client()
    run = register(http, world["key"])
    endpoint = f"{live.origin}/api/events" if transport == "http" else f"unix:{live.hub.socket_path}"
    content = _review(300 * 1024)
    _send(Emitter(endpoint, run["token"]), "review.md", content, step_id="post")
    [row] = _stored(live, run["run_id"])
    assert (row.name, row.step_id, row.media_type, row.content) == ("review.md", "post", "text/markdown", content)
    assert row.size == len(content.encode())
    viewer = live.client(world["users"]["viewer"])
    listed = viewer.get(f"/api/runs/{run['run_id']}/artifacts").json()["artifacts"]
    assert [(a["name"], a["size"], a["media_type"]) for a in listed] == [("review.md", row.size, "text/markdown")]
    assert "content" not in listed[0]
    got = viewer.get(f"/api/runs/{run['run_id']}/artifacts/review.md").json()["artifact"]
    assert got["content"] == content and got["step_id"] == "post"
    with live.engine.connect() as conn:
        kinds = conn.execute(select(run_events.c.kind, run_events.c.payload)
                             .where(run_events.c.run_id == run["run_id"])).all()
    assert [(k, p) for k, p in kinds] == [("artifact", {"name": "review.md", "media_type": "text/markdown",
                                                        "size": row.size})]


def test_a_secret_split_across_parts_is_redacted(live, world, keyring):
    secret = "hunter2-deploy-key-value"
    with live.engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=world["team"], name="DEPLOY_KEY", value=secret, actor_id=None)
    http = live.client()
    run = register(http, world["key"])
    head = "a" * (ARTIFACT_PART_BYTES - 10)
    content = head + secret + " and ghp_abcdefghijklmnop0123 too"
    parts = [content[:ARTIFACT_PART_BYTES - 2], content[ARTIFACT_PART_BYTES - 2:]]
    assert secret not in parts[0] and secret not in parts[1]
    lines = [line("artifact", "post", name="notes.txt", media_type="text/plain", upload="0123456789abcdef",
                  part=index, parts=2, content=part) for index, part in enumerate(parts)]
    assert post(http, run["token"], *lines)["accepted"] == 2
    live.flush()
    [row] = _rows(live.engine, run["run_id"])
    assert row.content == head + "[redacted] and [redacted] too"
    assert row.size == len(row.content.encode())


def test_the_emitter_redacts_before_sending(live, world, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "local-only-secret-value")
    monkeypatch.setenv("ARMATURE_REDACT_ENV", "MY_SECRET")
    http = live.client()
    run = register(http, world["key"])
    _send(Emitter(f"{live.origin}/api/events", run["token"]), "out.json", json.dumps({"k": "local-only-secret-value"}),
          "application/json")
    live.flush()
    assert _rows(live.engine, run["run_id"])[0].content == '{"k": "[redacted]"}'


def test_a_later_artifact_with_the_same_name_replaces_it(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("artifact", "a", name="review.md", media_type="text/markdown", content="first"))
    live.flush()
    post(http, run["token"], line("artifact", "b", name="review.md", media_type="text/plain", content="second"),
         line("artifact", "b", name="extra.json", media_type="application/json", content="{}"))
    viewer = live.client(world["users"]["viewer"])
    pending = viewer.get(f"/api/runs/{run['run_id']}/artifacts/review.md").json()["artifact"]
    assert (pending["content"], pending["media_type"]) == ("second", "text/plain")
    assert [a["name"] for a in viewer.get(f"/api/runs/{run['run_id']}/artifacts").json()["artifacts"]] == [
        "extra.json", "review.md"]
    live.flush()
    rows = {r.name: r for r in _rows(live.engine, run["run_id"])}
    assert set(rows) == {"review.md", "extra.json"}
    assert (rows["review.md"].content, rows["review.md"].step_id, rows["review.md"].size) == ("second", "b", 6)


def test_bad_and_oversize_artifacts_are_refused(live, world):
    http = live.client()
    run = register(http, world["key"])
    ok = {"name": "r.md", "media_type": "text/markdown", "content": "x"}
    bad = [
        {**ok, "name": "../etc/passwd"},
        {**ok, "name": ".hidden"},
        {**ok, "name": "a" * 101},
        {**ok, "media_type": "text/html"},
        {**ok, "content": 7},
        {**ok, "parts": 0},
        {**ok, "parts": 2, "part": 2, "upload": "0123456789abcdef"},
        {**ok, "parts": 2, "part": 0, "upload": "not hex"},
        {**ok, "parts": 513, "part": 0, "upload": "0123456789abcdef"},
        {**ok, "content": "x" * (MAX_ARTIFACT_BYTES + 1)},
    ]
    result = post(http, run["token"], *[line("artifact", None, **payload) for payload in bad[:-1]])
    assert result["accepted"] == 0
    big = "y" * (60 * 1024)
    parts = MAX_ARTIFACT_BYTES // len(big) + 1
    lines = [line("artifact", None, name="big.md", media_type="text/markdown", upload="fedcba9876543210",
                  part=index, parts=parts + 1, content=big) for index in range(parts)]
    for chunk in lines:
        post(http, run["token"], chunk)
    live.flush()
    assert _rows(live.engine, run["run_id"]) == []
    assert Emitter(f"{live.origin}/api/events", run["token"]).artifact("big.md", "z" * (MAX_ARTIFACT_BYTES + 1)) is False


def test_a_run_holds_a_limited_number_and_size_of_artifacts(live, world, monkeypatch):
    from armature.server import ingest
    monkeypatch.setattr(ingest, "MAX_ARTIFACTS_PER_RUN", 2)
    monkeypatch.setattr(ingest, "MAX_ARTIFACT_BYTES_PER_RUN", 10)
    http = live.client()
    run = register(http, world["key"])

    def send(name, content):
        return post(http, run["token"], line("artifact", None, name=name, media_type="text/plain",
                                             content=content))["accepted"]

    assert send("a.txt", "12345") == 1
    assert send("b.txt", "1234") == 1
    assert send("c.txt", "1") == 0
    assert send("a.txt", "1234567") == 0
    assert send("a.txt", "123456") == 1
    live.flush()
    assert {r.name: r.content for r in _rows(live.engine, run["run_id"])} == {"a.txt": "123456", "b.txt": "1234"}


def test_artifact_routes_hide_other_teams(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("artifact", None, name="review.md", media_type="text/markdown", content="# hi"))
    live.flush()
    outsider = live.client(world["users"]["outsider"])
    for path in ("/artifacts", "/artifacts/review.md"):
        assert outsider.get(f"/api/runs/{run['run_id']}{path}").status_code == 404
        assert live.client().get(f"/api/runs/{run['run_id']}{path}").status_code == 401
    viewer = live.client(world["users"]["viewer"])
    assert viewer.get(f"/api/runs/{run['run_id']}/artifacts/missing.md").status_code == 404
    assert viewer.get("/api/runs/nope/artifacts").status_code == 404


def test_an_artifact_frame_reaches_the_run_stream(live, world):
    http = live.client()
    run = register(http, world["key"])
    viewer = live.client(world["users"]["viewer"])
    with viewer.stream("GET", f"/api/runs/{run['run_id']}/stream") as raw:
        response = SSE(raw)
        response.until(lambda f: any(x.get("event") == "run" for x in f))
        post(http, run["token"], line("artifact", "post", name="review.md", media_type="text/markdown",
                                      content="# Review\n\nsecret-free"))
        frames = response.until(lambda f: any(x.get("event") == "run-event" for x in f))
    [frame] = [f for f in frames if f.get("event") == "run-event"]
    assert frame["data"]["kind"] == "artifact" and frame["data"]["step_id"] == "post"
    assert frame["data"]["payload"] == {"name": "review.md", "media_type": "text/markdown", "size": 21}


def test_outputs_route_returns_stored_outputs_and_cuts_huge_ones(live, world):
    http = live.client()
    run = register(http, world["key"])
    post(http, run["token"], line("step_start", "resolve"), line("step_end", "resolve", output={"title": "Fix crash"}),
         line("step_start", "verify"), line("step_end", "verify"))
    live.flush()
    with live.engine.begin() as conn:
        conn.execute(update(run_steps).where(run_steps.c.run_id == run["run_id"], run_steps.c.step_key == "verify")
                     .values(output="z" * (OUTPUT_SHOWN_BYTES + 10)))
    viewer = live.client(world["users"]["viewer"])
    outputs = viewer.get(f"/api/runs/{run['run_id']}/outputs").json()["outputs"]
    assert outputs["resolve"] == {"title": "Fix crash"}
    assert outputs["verify"]["_truncated"] is True and len(outputs["verify"]["value"]) == OUTPUT_SHOWN_BYTES
    assert live.client(world["users"]["outsider"]).get(f"/api/runs/{run['run_id']}/outputs").status_code == 404
