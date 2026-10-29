"""A real server on a free port, for SSE and socket tests that TestClient can't drive."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator

import httpx
import uvicorn
from sqlalchemy import insert

from armature.server import runners
from armature.server.app import create_app
from armature.server.auth import start_session
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import projects, teams
from tests.server.app_support import COST

COOKIE = "armature-local"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def socket_dir() -> str:
    path = tempfile.mkdtemp(prefix="arm")
    if len(path) > 80:
        shutil.rmtree(path, ignore_errors=True)
        path = tempfile.mkdtemp(prefix="arm", dir="/tmp")
    return path


class LiveServer:
    def __init__(self, engine, keyring, clock, *, socket: bool = True, **overrides):
        self.engine, self.keyring, self.clock = engine, keyring, clock
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.dir = socket_dir() if socket else None
        overrides.setdefault("require_totp", True)
        cfg = ServerConfig(database_url=str(engine.url), public_origin=self.origin,
                           events_socket=os.path.join(self.dir, "e.sock") if socket else "", **overrides)
        self.app = create_app(cfg, engine, keyring, clock=clock, password_cost=COST)
        self.hub = self.app.state.live
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                   lifespan="on", timeout_graceful_shutdown=2))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, coro, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coro, self.hub._loop).result(timeout)

    def flush(self) -> None:
        self.call(self.hub.writer.flush())

    def client(self, user_id: str | None = None, *, step_up: bool = True, **kwargs) -> httpx.Client:
        cookies = {}
        if user_id is not None:
            with self.engine.begin() as conn:
                token, _ = start_session(conn, self.keyring, user_id, self.clock(), ip=None, step_up=step_up)
            cookies[COOKIE] = token
        return httpx.Client(base_url=self.origin, cookies=cookies, timeout=10, **kwargs)

    def csrf(self, client: httpx.Client) -> dict[str, str]:
        return {"Origin": self.origin, "X-CSRF-Token": client.get("/api/session").json()["csrf"]}


def add_team(engine, clock, name: str) -> tuple[str, str]:
    team_id, project_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(teams).values(id=team_id, name=name, created_at=clock()))
        conn.execute(insert(projects).values(id=project_id, team_id=team_id, name=f"{name}-app", created_at=clock()))
    return team_id, project_id


def add_runner_key(engine, keyring, clock, team_id: str) -> str:
    with engine.begin() as conn:
        _, key = runners.create_key(conn, keyring, team_id=team_id, name="ci", actor_id=None, now=clock(), ip=None)
    return key


def register(http: httpx.Client, key: str, **body) -> dict:
    body = {"workflow": "pr-review", "actor": "octocat", "trigger_kind": "webhook", "trigger_ref": {"pr": 1}, **body}
    response = http.post("/api/runner/runs", json=body, headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200, response.text
    return response.json()


def line(kind: str, step_id: str | None = None, *, event_token: str | None = None, **payload) -> bytes:
    event = {"v": 1, "kind": kind, "ts": "2026-10-23T12:00:00+00:00", "step_id": step_id, "pid": 1,
             "payload": payload}
    if event_token is not None:
        event["token"] = event_token
    return json.dumps(event).encode()


def post(http: httpx.Client, token: str, *lines: bytes) -> dict:
    response = http.post("/api/events", content=b"\n".join(lines) + b"\n",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"})
    assert response.status_code == 202, response.text
    return response.json()


class SSE:
    """Parsed frames from one streamed response: {id, event, data} with data decoded, comments as {"comment"}."""

    def __init__(self, response: httpx.Response):
        self._lines = response.iter_lines()

    def __iter__(self) -> Iterator[dict]:
        frame: dict = {}
        for text in self._lines:
            if text == "":
                if frame:
                    if "data" in frame:
                        frame["data"] = json.loads(frame["data"])
                    yield frame
                frame = {}
            elif text.startswith(":"):
                yield {"comment": text[1:].strip()}
            else:
                name, _, value = text.partition(":")
                frame[name] = value.removeprefix(" ")

    def until(self, done) -> list[dict]:
        seen: list[dict] = []
        for frame in self:
            seen.append(frame)
            if done(seen):
                break
        return seen
