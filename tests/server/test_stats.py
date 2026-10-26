"""Overview and Trends aggregates, their scoping and day boundaries, and the display settings."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import insert, update

from armature.server import display, stats
from armature.server.db import new_id
from armature.server.db.schema import run_steps, runs, teams, workflows
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _team(conn, name):
    team_id = new_id()
    conn.execute(insert(teams).values(id=team_id, name=name, created_at=NOW))
    return team_id


def _run(conn, team_id, workflow, status, started, finished=None, cost=None, trigger_ref=None, error=None,
         summary=None):
    run_id = new_id()
    conn.execute(insert(runs).values(id=run_id, team_id=team_id, workflow_name=workflow, status=status,
                                     started_at=started, finished_at=finished,
                                     cost_usd=Decimal(str(cost)) if cost is not None else None,
                                     trigger_ref=trigger_ref, error=error, summary=summary))
    return run_id


def _step(conn, run_id, step_id, started, finished, parent=None, cost=None, error=None):
    conn.execute(insert(run_steps).values(run_id=run_id, step_key=f"{parent}/{step_id}" if parent else step_id,
                                          step_id=step_id, parent_step=parent, status="succeeded",
                                          started_at=started, finished_at=finished,
                                          cost_usd=Decimal(str(cost)) if cost is not None else None, error=error))


@pytest.fixture
def seeded(engine, keyring):
    clock = Clock(NOW)
    with engine.begin() as conn:
        own, other = _team(conn, "own"), _team(conn, "other")
        conn.execute(insert(workflows).values(id=new_id(), team_id=own, name="unused", current_version=1,
                                              created_at=NOW, updated_at=NOW))
        review = _run(conn, own, "code-review", "succeeded", at(23, 7), at(23, 7, 10), 1.5, {},
                      summary={"verdict": "approve"})
        _step(conn, review, "resolve", at(23, 7), at(23, 7, 2))
        _step(conn, review, "reviews", at(23, 7, 2), at(23, 7, 8))
        _step(conn, review, "security", at(23, 7, 2), at(23, 7, 5), parent="reviews", cost=0.5)
        _step(conn, review, "beauty", at(23, 7, 2), at(23, 7, 3), parent="reviews", cost=0.25)
        _run(conn, own, "code-review", "succeeded", at(23, 8), at(23, 8, 1), 0.25, {"mode": "discuss"})
        # 05:30 UTC is 23:30 the day before in Denver.
        late = _run(conn, own, "jira-work", "failed", at(23, 5, 30), at(23, 5, 40), 2, None,
                    error="1 step(s) failed: implement")
        _step(conn, late, "implement", at(23, 5, 30), at(23, 5, 40), error="AuthenticationError provider_auth")
        running = _run(conn, own, "jira-work", "running", at(23, 11))
        lost = _run(conn, own, "jira-work", "lost", at(20, 9))
        _run(conn, own, "jira-work", "succeeded", at(18, 9), at(18, 9, 30), 7)
        _step(conn, lost, "timeout", at(20, 9), at(20, 9, 1), error="TimeoutExpired")
        foreign = _run(conn, other, "code-review", "failed", at(23, 9), at(23, 9, 5), 100,
                       error="403 Forbidden")
        _step(conn, foreign, "resolve", at(23, 9), at(23, 9, 5))
    viewer, _ = add_user(engine, keyring, clock, "vee")
    add_membership(engine, clock, viewer, "team", own, "viewer")
    admin, _ = add_user(engine, keyring, clock, "root", is_admin=True)
    return {"clock": clock, "own": own, "other": other, "viewer": viewer, "admin": admin, "running": running,
            "late": late, "lost": lost, "review": review}


def _client(engine, keyring, seeded, who, step_up=True):
    client = make_client(engine, keyring, seeded["clock"])
    csrf = sign_in_as(client, engine, keyring, seeded["clock"], seeded[who], step_up=step_up)
    return client, csrf


def test_percentile_interpolates_like_percentile_cont():
    assert stats.percentile([], 0.5) is None
    assert stats.percentile([5], 0.9) == 5
    assert stats.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert stats.percentile([10, 0, 20, 30, 40], 0.9) == pytest.approx(36)


def test_day_boundaries_follow_the_zone():
    from zoneinfo import ZoneInfo
    denver = ZoneInfo("America/Denver")
    assert stats.local_day(at(23, 5, 30), denver).isoformat() == "2026-10-22"
    assert stats.local_day(at(23, 6, 0), denver).isoformat() == "2026-10-23"
    assert stats.day_start(datetime(2026, 10, 23).date(), denver) == at(23, 6)


def test_overview_counts_only_the_viewers_teams(engine, keyring, seeded):
    client, _ = _client(engine, keyring, seeded, "viewer")
    body = client.get("/api/overview").json()
    assert body["counts"] == {"running": 1, "runs_24h": 4, "failed_24h": 1}
    assert [r["id"] for r in body["running"]] == [seeded["running"]]
    assert body["spend"]["today"] == pytest.approx(1.75)
    assert body["spend"]["avg_7d"] == pytest.approx(9 / 7)
    assert body["reviews_today"] == 1
    assert body["reevaluations_today"] == 1
    assert body["time"]["agent_today"] == 480
    assert body["time"]["agent_all"] == 480 + 600 + 60
    assert body["time"]["man_hours_today"] == pytest.approx(0.13)
    assert len(body["recent"]) == 6 and body["recent"][0]["id"] == seeded["running"]
    kinds = {a["kind"]: a for a in body["alerts"]}
    assert set(kinds) == {"failed", "credential"}
    assert kinds["failed"]["workflow"] == "jira-work" and kinds["failed"]["run_id"] == seeded["late"]
    assert kinds["credential"]["run_id"] == seeded["late"]
    assert all(r["team_id"] == seeded["own"] for r in body["recent"])
    assert client.get("/api/overview", params={"team": seeded["other"]}).status_code == 404
    assert client.get("/api/overview", params={"team": "missing"}).status_code == 404


def test_admin_sees_every_team_and_can_narrow(engine, keyring, seeded):
    client, _ = _client(engine, keyring, seeded, "admin")
    body = client.get("/api/overview").json()
    assert body["spend"]["today"] == pytest.approx(101.75)
    assert body["counts"]["failed_24h"] == 2
    narrowed = client.get("/api/overview", params={"team": seeded["other"]}).json()
    assert narrowed["counts"] == {"running": 0, "runs_24h": 1, "failed_24h": 1}
    assert narrowed["alerts"][0]["kind"] == "failed"
    assert {a["kind"] for a in narrowed["alerts"]} == {"failed", "credential"}


def test_trends_buckets_by_local_day(engine, keyring, seeded):
    client, _ = _client(engine, keyring, seeded, "viewer")
    body = client.get("/api/trends", params={"days": 7}).json()
    assert body["days"][0] == "2026-10-17" and body["days"][-1] == "2026-10-23"
    assert body["workflows"] == ["jira-work", "code-review"]
    assert body["runs_by_workflow"]["code-review"][-1] == 2
    assert body["runs_by_workflow"]["jira-work"][-2:] == [1, 1]
    assert body["spend_by_workflow"]["jira-work"][-2] == pytest.approx(2)
    assert body["failure_rate"][-2] == {"finished": 1, "failed": 1, "rate": 1.0}
    assert body["failure_rate"][-1] == {"finished": 2, "failed": 0, "rate": 0.0}
    assert body["failure_rate"][-4]["failed"] == 1
    assert body["verdicts_per_day"] == {"approve": [0, 0, 0, 0, 0, 0, 1]}
    review = body["duration_p50_p90_by_workflow"]["code-review"]
    assert review["n"] == 2 and review["p50"] == pytest.approx(330) and review["daily_p50"][-1] == pytest.approx(330)
    assert body["slowest_steps"][0] == {"workflow": "jira-work", "step": "implement", "n": 1, "p50": 600, "p90": 600}
    assert [r["role"] for r in body["cost_by_role"]] == ["security", "beauty"]
    assert body["cost_by_role"][0]["cost"] == pytest.approx(0.5) and body["cost_by_role"][0]["p90"] == 180
    assert body["agent_hours_per_day"][-1] == pytest.approx(0.13)
    assert body["man_hours_per_day"][-2] == pytest.approx(0.17)
    assert client.get("/api/trends", params={"days": 0}).status_code == 400
    assert client.get("/api/trends", params={"team": seeded["other"]}).status_code == 404


def test_display_settings_need_admin_and_step_up(engine, keyring, seeded):
    viewer, viewer_csrf = _client(engine, keyring, seeded, "viewer")
    put = {"scope": "instance", "key": "display.timezone", "value": "UTC"}
    assert viewer.post("/api/settings", json=put, headers=mutation(viewer_csrf)).status_code == 403
    stale, stale_csrf = _client(engine, keyring, seeded, "admin", step_up=False)
    refused = stale.post("/api/settings", json=put, headers=mutation(stale_csrf))
    assert refused.status_code == 403 and refused.json()["error"] == "step_up_required"
    admin, csrf = _client(engine, keyring, seeded, "admin")
    for bad in ({"key": "display.timezone", "value": "Mars/Base"},
                {"key": "display.human_multiplier", "value": 0},
                {"key": "display.human_multiplier", "value": True},
                {"key": "display.show_cost", "value": "no"},
                {"key": "display.color", "value": 1},
                {"key": "display.workflow_labels", "value": {"a": {"label": "Same"}, "b": {"label": "same"}}},
                {"key": "display.workflow_labels", "value": {"a": {"color": "red"}}},
                {"key": "display.workflow_labels", "value": {"a": {"hidden": "yes"}}}):
        assert admin.post("/api/settings", json={"scope": "instance", **bad}, headers=mutation(csrf)).status_code == 400
    team_scoped = {**put, "scope": seeded["own"]}
    assert admin.post("/api/settings", json=team_scoped, headers=mutation(csrf)).status_code == 400
    before = viewer.get("/api/display").json()
    assert before["display"]["timezone"] == "America/Denver"
    assert before["workflows"] == ["code-review", "jira-work", "unused"]
    assert "pr-merged" not in before["display"]["workflow_labels"]
    assert admin.get("/api/display").json()["display"]["workflow_labels"]["pr-merged"]["hidden"] is True
    for key, value in (("display.timezone", "UTC"), ("display.show_cost", False), ("display.human_multiplier", 10),
                       ("display.workflow_labels", {"code-review": {"label": " Review ", "color": "s2"}})):
        response = admin.post("/api/settings", json={"scope": "instance", "key": key, "value": value},
                              headers=mutation(csrf))
        assert response.status_code == 200, response.text
    shown = viewer.get("/api/display").json()["display"]
    assert shown == {"show_cost": False, "show_man_hours": True, "human_multiplier": 10, "timezone": "UTC",
                     "workflow_labels": {"code-review": {"label": "Review", "color": "s2", "hidden": False}},
                     "hidden_actors": []}
    body = viewer.get("/api/overview").json()
    assert body["spend"] is None and all(r["cost_usd"] is None for r in body["recent"])
    assert body["time"]["man_hours_today"] == pytest.approx(round(1080 / 3600 * 10, 2))
    trend = viewer.get("/api/trends", params={"days": 7}).json()
    assert trend["spend_by_workflow"] is None and trend["cost_by_role"][0]["cost"] is None
    assert trend["runs_by_workflow"]["jira-work"][-1] == 2


def test_hidden_actors_check():
    assert display.check("display.hidden_actors", ["smoke", "bot", "smoke"], None) == ["smoke", "bot"]
    assert display.check("display.hidden_actors", [], None) == []
    assert display.check("display.hidden_actors", ["a"] * 60, None) == ["a"]
    for bad in ("smoke", [""], [1], ["x" * 192], ["bad\nname"], [f"a{n}" for n in range(51)]):
        with pytest.raises(display.Invalid):
            display.check("display.hidden_actors", bad, None)
    with pytest.raises(display.Invalid):
        display.check("display.hidden_actors", ["smoke"], "team")


def test_signed_out_reads_are_refused(engine, keyring, seeded):
    client = make_client(engine, keyring, seeded["clock"])
    for path in ("/api/overview", "/api/trends", "/api/display"):
        assert client.get(path).status_code == 401


def test_hidden_workflows_leave_run_lists_unless_asked_for(engine, keyring, seeded):
    with engine.begin() as conn:
        merged = _run(conn, seeded["own"], "pr-merged", "running", at(23, 11, 30))
        unnamed = _run(conn, seeded["own"], None, "succeeded", at(23, 10), at(23, 10, 1))
    viewer, _ = _client(engine, keyring, seeded, "viewer")

    def listed(**params):
        return {r["id"] for r in viewer.get("/api/runs", params=params).json()["runs"]}

    default = listed()
    assert merged not in default and unnamed in default and seeded["review"] in default
    assert merged in listed(include_hidden=1)
    assert listed(workflows="pr-merged") == {merged}
    reviews = listed(workflows="code-review")
    assert len(reviews) == 2 and listed(workflows="pr-merged, code-review") == {merged} | reviews
    assert listed(workflow="pr-merged") == {merged}
    overview = viewer.get("/api/overview").json()
    assert merged not in {r["id"] for r in overview["recent"] + overview["running"]}
    assert overview["counts"]["running"] == 2
    assert viewer.get("/api/trends", params={"days": 7}).json()["runs_by_workflow"]["pr-merged"][-1] == 1

    admin, csrf = _client(engine, keyring, seeded, "admin")
    unhide = {"scope": "instance", "key": "display.workflow_labels", "value": {"jira-work": {"hidden": True}}}
    assert admin.post("/api/settings", json=unhide, headers=mutation(csrf)).status_code == 200
    now_listed = listed()
    assert merged in now_listed and not any(r in now_listed for r in listed(workflows="jira-work"))
    assert viewer.get("/api/overview").json()["running"][0]["id"] == merged


def test_hidden_actors_leave_the_run_list_unless_asked_for(engine, keyring, seeded):
    with engine.begin() as conn:
        smoke = _run(conn, seeded["own"], "code-review", "succeeded", at(23, 11), at(23, 11, 1))
        by_actor = _run(conn, seeded["own"], "code-review", "succeeded", at(23, 11, 2), at(23, 11, 3))
        person = _run(conn, seeded["own"], "code-review", "succeeded", at(23, 11, 4), at(23, 11, 5))
        conn.execute(update(runs).where(runs.c.id == smoke).values(triggered_by="smoke", actor="dev"))
        conn.execute(update(runs).where(runs.c.id == by_actor).values(actor="smoke"))
        conn.execute(update(runs).where(runs.c.id == person).values(triggered_by="dev", actor="smoke"))
    admin, csrf = _client(engine, keyring, seeded, "admin")
    hide = {"scope": "instance", "key": "display.hidden_actors", "value": ["smoke"]}
    assert admin.post("/api/settings", json=hide, headers=mutation(csrf)).status_code == 200
    viewer, _ = _client(engine, keyring, seeded, "viewer")
    assert viewer.get("/api/display").json()["display"]["hidden_actors"] == ["smoke"]

    def listed(**params):
        return {r["id"] for r in viewer.get("/api/runs", params=params).json()["runs"]}

    default = listed()
    assert smoke not in default and by_actor not in default
    assert person in default and seeded["review"] in default
    assert {smoke, by_actor, person} <= listed(include_hidden=1)
    assert smoke not in listed(workflows="code-review") and smoke not in listed(workflow="code-review")
