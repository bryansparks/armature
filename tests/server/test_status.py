"""The Status page: a host command's Markdown, refreshed off the event loop, redacted, and served to anyone signed in."""
import sys
import textwrap
import threading
import time

import pytest

from armature.server import secrets as secret_store
from armature.server.config import StatusConfig
from armature.server.status import MARKDOWN_MAX, StatusHook
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    sign_in_as,
)
from tests.server.live_support import add_team

FAKE = textwrap.dedent("""
    import os, pathlib, sys, time
    control = pathlib.Path(sys.argv[1]).read_text().split("|", 2)
    time.sleep(float(control[0]))
    sys.stdout.write(control[2])
    sys.stderr.write("status broke\\n" if control[1] != "0" else "")
    pathlib.Path(sys.argv[2]).write_text(",".join(sorted(os.environ)))
    sys.exit(int(control[1]))
""")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "status.py"
    script.write_text(FAKE)
    control = tmp_path / "control"
    env_file = tmp_path / "env"

    class Fake:
        command = (sys.executable, str(script), str(control), str(env_file))

        @staticmethod
        def set(text, exit_code=0, sleep=0.0):
            control.write_text(f"{sleep}|{exit_code}|{text}")

        @staticmethod
        def env():
            return env_file.read_text().split(",")

    Fake.set("# Host\n\nAll quiet.\n")
    return Fake


def _hook(engine, keyring, clock, fake, **cfg):
    return StatusHook(engine, keyring, StatusConfig(command=list(fake.command), **cfg), clock=clock)


def test_status_config_validates():
    assert StatusConfig(command=["/bin/echo"]).timeout_seconds == 20
    for bad in ({"command": []}, {"command": ["/bin/echo"], "interval_seconds": 1},
                {"command": ["/bin/echo"], "timeout_seconds": 0}):
        with pytest.raises(ValueError):
            StatusConfig(**bad)


def test_refresh_keeps_redacted_markdown(engine, keyring, clock, fake):
    with engine.begin() as conn:
        secret_store.set_secret(conn, keyring, team_id=None, name="SLACK", value="sekrit-value", actor_id=None)
    fake.set("token sekrit-value and xoxb-123-abc and ghp_abcdefgh\n")
    hook = _hook(engine, keyring, clock, fake)
    assert hook.refresh() is True
    snap = hook.snapshot()
    assert "sekrit" not in snap["markdown"] and "xoxb-" not in snap["markdown"] and "ghp_" not in snap["markdown"]
    assert snap["markdown"].count("[redacted]") == 3
    assert snap["updated_at"] == clock() and snap["error"] is None
    assert set(fake.env()) <= {"PATH", "HOME", "LANG", "__CF_USER_TEXT_ENCODING"}


def test_a_failed_or_slow_refresh_keeps_the_last_good_output(engine, keyring, clock, fake):
    hook = _hook(engine, keyring, clock, fake, timeout_seconds=1)
    assert hook.refresh()
    first = hook.snapshot()
    clock.advance(minutes=1)
    fake.set("half", exit_code=1)
    assert hook.refresh() is False
    snap = hook.snapshot()
    assert snap["markdown"] == first["markdown"] and snap["updated_at"] == first["updated_at"]
    assert snap["error"] == "The status command exited 1: status broke"
    fake.set("late", sleep=10)
    started = time.monotonic()
    assert hook.refresh() is False
    assert time.monotonic() - started < 8
    assert "did not finish within 1 s" in hook.snapshot()["error"]
    assert hook.snapshot()["markdown"] == first["markdown"]


def test_output_is_capped(engine, keyring, clock, fake):
    fake.set("x" * (MARKDOWN_MAX + 5000))
    hook = _hook(engine, keyring, clock, fake)
    assert hook.refresh()
    assert len(hook.snapshot()["markdown"].encode()) <= MARKDOWN_MAX


def test_the_loop_runs_on_its_own_thread_and_stops(engine, keyring, clock, fake):
    hook = _hook(engine, keyring, clock, fake)
    seen = []
    original = hook.refresh

    def refresh():
        seen.append(threading.current_thread().name)
        return original()

    hook.refresh = refresh
    hook.start()
    deadline = time.monotonic() + 10
    while hook.snapshot()["markdown"] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    hook.stop()
    assert seen and set(seen) == {"armature-status"}
    assert hook.snapshot()["markdown"].startswith("# Host")


def test_route_answers_while_the_command_runs(engine, keyring, clock, fake):
    user_id, _ = add_user(engine, keyring, clock, "admin", is_admin=True)
    fake.set("slow", sleep=5)
    client = make_client(engine, keyring, clock, events_socket="",
                         status={"command": list(fake.command), "timeout_seconds": 10, "interval_seconds": 30})
    sign_in_as(client, engine, keyring, clock, user_id, step_up=False)
    with client:
        started = time.monotonic()
        body = client.get("/api/status").json()
        assert time.monotonic() - started < 2
    assert body == {"configured": True, "markdown": None, "updated_at": None, "error": None}


def test_route_is_for_admins_and_reports_unconfigured(engine, keyring, clock):
    client = make_client(engine, keyring, clock)
    assert client.get("/api/status").status_code == 401
    viewer, _ = add_user(engine, keyring, clock, "viewer")
    sign_in_as(client, engine, keyring, clock, viewer, step_up=False)
    assert client.get("/api/status").status_code == 403
    client.cookies.clear()
    admin, _ = add_user(engine, keyring, clock, "admin", is_admin=True)
    sign_in_as(client, engine, keyring, clock, admin, step_up=False)
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "markdown": None, "updated_at": None, "error": None}


def test_route_hides_configured_status_from_team_roles(engine, keyring, clock, fake):
    team, _ = add_team(engine, clock, "mobile")
    lead, _ = add_user(engine, keyring, clock, "lead")
    add_membership(engine, clock, lead, "team", team, "team_lead")
    client = make_client(engine, keyring, clock, status={"command": list(fake.command)})
    sign_in_as(client, engine, keyring, clock, lead, step_up=True)
    assert client.get("/api/status").status_code == 403
