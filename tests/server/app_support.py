"""Shared helpers for the server app tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import insert

from armature.server.app import create_app
from armature.server.auth import start_session
from armature.server.config import ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import memberships, users
from armature.server.passwords import hash_password
from armature.server.users import totp_aad

ORIGIN = "https://arm.test"
PASSWORD = "correct horse battery"
COST = 2 ** 4


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)

    def code(self, secret: str) -> str:
        return pyotp.TOTP(secret).at(self.now)


def make_client(engine, keyring, clock, *, peer=("testclient", 50000), static_dir=None, oidc_transport=None,
                worker_interval=None, **overrides) -> TestClient:
    overrides.setdefault("require_totp", True)
    cfg = ServerConfig(database_url=str(engine.url), public_origin=overrides.pop("public_origin", ORIGIN),
                       **overrides)
    extra = {"static_dir": static_dir} if static_dir else {}
    app = create_app(cfg, engine, keyring, clock=clock, password_cost=COST, oidc_transport=oidc_transport,
                     worker_interval=worker_interval, **extra)
    base = cfg.public_origin
    return TestClient(app, base_url=base, client=peer, follow_redirects=False)


def add_user(engine, keyring, clock, username: str, *, is_admin: bool = False, confirmed: bool = True,
             disabled: bool = False) -> tuple[str, str]:
    user_id = new_id()
    secret = pyotp.random_base32()
    with engine.begin() as conn:
        conn.execute(insert(users).values(
            id=user_id, username=username, display_name=username, password_hash=hash_password(PASSWORD, cost=COST),
            totp_secret_enc=keyring.seal(secret, totp_aad(user_id), label="totp"), last_totp_step=0,
            is_admin=is_admin, disabled=disabled, created_at=clock(),
            totp_confirmed_at=clock() if confirmed else None))
    return user_id, secret


def add_membership(engine, clock, user_id: str, scope_kind: str, scope_id: str, role: str,
                   source: str = "manual") -> str:
    membership_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(memberships).values(id=membership_id, user_id=user_id, scope_kind=scope_kind,
                                                scope_id=scope_id, role=role, source=source, created_at=clock()))
    return membership_id


def mutation(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


def login(client: TestClient, username: str, code: str | None, password: str = PASSWORD, headers=None):
    csrf = client.get("/api/session").json()["csrf"]
    body = {"username": username, "password": password}
    if code is not None:
        body["totp"] = code
    return client.post("/api/login", json=body, headers={**mutation(csrf), **(headers or {})})


def sign_in_as(client: TestClient, engine, keyring, clock, user_id: str, *, step_up: bool = True) -> str:
    """A session made directly, for tests that are about what a session may do rather than how it starts."""
    with engine.begin() as conn:
        token, csrf = start_session(conn, keyring, user_id, clock(), ip=None, step_up=step_up)
    client.cookies.set("__Secure-armature", token, domain="arm.test", path="/")
    return csrf
