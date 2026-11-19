"""The insights-demo seed: refusals beside real teams, replacing its own history, and data on every Insights screen."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, insert, select, update
from typer.testing import CliRunner

from armature.cli import app
from armature.server.db import make_engine, migrate, new_id
from armature.server.db.schema import (
    FIX_STATES,
    audit,
    fix_proposals,
    gates,
    gh_repos,
    jobs,
    people,
    runs,
    settings,
    teams,
)
from armature.server.insights import demo, metrics
from tests.server.live_support import add_team

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TABLES = (teams, settings, *demo.INSERT_ORDER, fix_proposals, gates, jobs)
KINDS = {"dropped_flag_bug", "specialist_dropped_bugs", "rounds_rising", "conflict_file", "waiting_on_review"}


def _counts(engine):
    with engine.connect() as conn:
        return {t.name: conn.execute(select(func.count()).select_from(t)).scalar() for t in TABLES}


def _team_id(engine, slug):
    with engine.connect() as conn:
        return conn.execute(select(teams.c.id).where(teams.c.slug == slug)).scalar_one()


def test_seeding_fills_every_insights_screen(sqlite_engine):
    counts = demo.seed(sqlite_engine, "demo", NOW)
    assert counts["pull_requests"] > 300 and counts["flags"] > 300 and counts["llm_calls"] > 1000
    team = _team_id(sqlite_engine, "demo")
    with sqlite_engine.connect() as conn:
        marked = conn.execute(select(settings.c.value).where(settings.c.team_id == team,
                                                             settings.c.key == demo.DEMO_SETTING)).scalar()
        repos = {r.full_name: r.id for r in conn.execute(select(gh_repos).where(gh_repos.c.team_id == team))}
        assert marked == {"seeded_at": NOW.isoformat()}
        assert set(repos) == {demo.IOS, demo.ANDROID, demo.SERVICE}
        for days in metrics.DAYS:
            view = metrics.overview(conn, team, NOW, days, None, with_people=True)
            assert all(m["value"] is not None and m["previous"] is not None for m in view["metrics"]), days
            assert view["metrics"][0]["value"] and view["metrics"][0]["previous"]
            assert view["people"] and view["attention"]
        attention = metrics.overview(conn, team, NOW, 30, None, with_people=True)["attention"]
        aggregate = metrics.overview(conn, team, NOW, 30, None, with_people=False)["attention"]
        people_page = metrics.people_view(conn, team, NOW, 30, None, "review")
        found = metrics.findings_screen(conn, team, NOW, 30, repo_id=None, category=None, person_id=None)
        models = metrics.models_view(conn, team, metrics.window(NOW, 30))
        by_repo = {name: metrics.repo_view(conn, team, NOW, 30, repo_id) for name, repo_id in repos.items()}
        person = metrics.person_view(conn, team, NOW, 30, people_page["rows"][0]["id"])
        fixes = metrics.fixes_view(conn, team, NOW)
        open_gates = conn.execute(select(gates.c.id).where(gates.c.team_id == team, gates.c.state == "open")).all()
    assert {i["kind"] for i in attention} == KINDS
    item = {i["kind"]: i for i in attention}
    assert item["rounds_rising"]["repo"] == demo.ANDROID
    assert (item["conflict_file"]["repo"], item["conflict_file"]["path"]) == (demo.ANDROID, demo.GRADLE)
    assert item["waiting_on_review"]["person"] == "Hannah Kowalski"
    waiting = next(i for i in aggregate if i["kind"] == "waiting_on_review")
    assert "person" not in waiting and waiting["count"] >= 2
    assert item["specialist_dropped_bugs"] == {"kind": "specialist_dropped_bugs", "specialist": "concurrency",
                                               "count": 2}
    assert len(people_page["rows"]) == len(demo.PEOPLE) and people_page["armature_reviews"] > 0
    assert "Armature" not in {r["name"] for r in people_page["rows"]}
    assert found["categories"] and found["specialists"] and len(found["dropped_became_bugs"]) == 2
    assert models["spend_usd"]["value"] > 0 and models["cost_per_reviewed_pr_usd"]["value"] > 0
    assert {m["name"] for m in models["by_model"]} >= {"claude-opus-4-6", "gemini-2.5-pro"}
    for view in by_repo.values():
        assert view["stages"]["pr"] is not None and view["hotspots"] and view["push_kinds"]
        assert all(t["value"] is not None for t in view["tiles"] if t["id"] != "conflict_hours")
    assert person["merged_prs"] and person["reviews"]
    assert {i["state"] for listed in fixes.values() for i in listed} == set(FIX_STATES)
    assert len(fixes["waiting"]) == len(open_gates) == 2 and all(i["diff"]["lines"] for i in fixes["waiting"])
    [merged] = [i for i in fixes["done"] if i["state"] == "merged"]
    assert merged["flags_before"] is not None and merged["flags_after"] is not None


def test_seeding_again_replaces_the_demo_history(sqlite_engine):
    demo.seed(sqlite_engine, "demo", NOW)
    first = _counts(sqlite_engine)
    demo.seed(sqlite_engine, "demo", NOW)
    assert _counts(sqlite_engine) == first and first["teams"] == 1


def test_seeding_again_keeps_runs_the_demo_didnt_make(sqlite_engine):
    demo.seed(sqlite_engine, "demo", NOW)
    team = _team_id(sqlite_engine, "demo")
    mine = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id=mine, team_id=team, status="succeeded", started_at=NOW))
    demo.seed(sqlite_engine, "demo", NOW)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(runs.c.id).where(runs.c.id == mine)).scalar() == mine


def test_a_slug_that_names_two_teams_is_refused(sqlite_engine):
    add_team(sqlite_engine, lambda: NOW, "demo")
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=new_id(), name="Demo two", slug="demo", created_at=NOW))
    before = _counts(sqlite_engine)
    with pytest.raises(demo.Refused, match="could mean either"):
        demo.seed(sqlite_engine, "demo", NOW, force=True)
    assert _counts(sqlite_engine) == before


def test_only_a_team_scope_marker_makes_a_demo_team(sqlite_engine):
    team, project = add_team(sqlite_engine, lambda: NOW, "mobile")
    with sqlite_engine.begin() as conn:
        conn.execute(update(teams).where(teams.c.id == team).values(slug="mobile"))
        conn.execute(insert(settings).values(team_id=team, project_id=project, key=demo.DEMO_SETTING,
                                             value={"seeded_at": NOW.isoformat()}, updated_at=NOW))
    before = _counts(sqlite_engine)
    with pytest.raises(demo.Refused, match="isn't a demo team"):
        demo.seed(sqlite_engine, "mobile", NOW, force=True)
    with pytest.raises(demo.Refused, match="aren't demo teams"):
        demo.seed(sqlite_engine, "demo", NOW)
    assert _counts(sqlite_engine) == before


def test_a_team_that_isnt_a_demo_team_is_never_seeded(sqlite_engine):
    add_team(sqlite_engine, lambda: NOW, "mobile")
    before = _counts(sqlite_engine)
    with pytest.raises(demo.Refused, match="isn't a demo team"):
        demo.seed(sqlite_engine, "mobile", NOW, force=True)
    with pytest.raises(demo.Refused, match="isn't a slug"):
        demo.seed(sqlite_engine, "Demo Team", NOW, force=True)
    assert _counts(sqlite_engine) == before


@pytest.fixture
def config(tmp_path):
    url = f"sqlite:///{tmp_path / 'server.db'}"
    migrate(url)
    path = tmp_path / "server.yaml"
    path.write_text(f"database_url: {url}\n")
    engine = make_engine(url)
    team, _ = add_team(engine, lambda: NOW, "mobile")
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=new_id(), team_id=team, status="succeeded", started_at=NOW))
    yield path, engine, team
    engine.dispose()


def _cli(path, *extra):
    return CliRunner().invoke(app, ["server", "insights-demo", "--team", "demo", "--config", str(path), *extra])


def test_the_command_refuses_beside_a_real_team_and_writes_nothing(config):
    path, engine, _ = config
    before = _counts(engine)
    with engine.connect() as conn:
        audited = conn.execute(select(func.count()).select_from(audit)).scalar()
    result = _cli(path)
    assert result.exit_code == 1
    assert "teams that aren't demo teams (mobile)" in result.output and "--force" in result.output
    assert _counts(engine) == before
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(audit)).scalar() == audited


def test_force_seeds_beside_a_real_team_and_leaves_it_alone(config):
    path, engine, mobile = config
    result = _cli(path, "--force")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Seeded demo team demo: ")
    seeded = _team_id(engine, "demo")
    with engine.connect() as conn:
        assert conn.execute(select(gh_repos.c.team_id).distinct()).scalars().all() == [seeded]
        assert conn.execute(select(people.c.team_id).distinct()).scalars().all() == [seeded]
        assert conn.execute(select(func.count()).select_from(runs).where(runs.c.team_id == mobile)).scalar() == 1
    again = _cli(path)
    assert again.exit_code == 1 and "teams that aren't demo teams (mobile)" in again.output
