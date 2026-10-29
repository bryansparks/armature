import itertools

import pytest
from sqlalchemy import Update, insert, select, update

from armature.server import risk
from armature.server.db import new_id
from armature.server.db.schema import runs
from armature.server.defs import DefError, put_setting
from tests.server.app_support import Clock
from tests.server.live_support import add_team


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(engine, clock):
    team, project = add_team(engine, clock, "mobile")
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(runs).values(id=run_id, team_id=team, project_id=project, status="running",
                                         started_at=clock()))
    return {"team": team, "project": project, "run": run_id}


@pytest.mark.parametrize("path, level, label", [
    ("armature/server/migrations/versions/0020_x.py", "high", "migrations and schema"),
    ("db/schema.rb", "high", "migrations and schema"),
    ("sql/001_init.SQL", "high", "migrations and schema"),
    ("app/src/main/java/OAuthHelper.kt", "high", "auth, sessions, and crypto"),
    ("Sources/Keychain/Store.swift", "high", "auth, sessions, and crypto"),
    ("web/payments/refund.ts", "high", "payments"),
    ("Dockerfile.ci", "high", "infrastructure and CI"),
    (".github/workflows/test.yml", "high", "infrastructure and CI"),
    ("infra/main.tf", "high", "infrastructure and CI"),
    ("app/src/main/AndroidManifest.xml", "high", "permission manifests"),
    ("App/App.entitlements", "high", "permission manifests"),
    ("App/Info.plist", "high", "permission manifests"),
    ("auth/session.py", "high", "auth, sessions, and crypto"),
    ("Sources/Login/AuthStore.swift", "high", "auth, sessions, and crypto"),
    ("db/migrations/x.sql", "high", "migrations and schema"),
    ("package-lock.json", "medium", "dependencies"),
    ("app/build.gradle.kts", "medium", "dependencies"),
    ("App.xcodeproj/project.pbxproj", "medium", "build configuration"),
    ("Makefile", "medium", "build configuration"),
])
def test_each_default_floor(path, level, label):
    assert risk.floor([path, "README.md"]) == (level, [{"source": "floor", "text": f"{label}: {path}"}])


@pytest.mark.parametrize("path", ["web/src/router.tsx", "nlp/tokenizer.py", "docs/author.md",
                                  "app/src/test/httpClientTests.kt", "server/routes/users.py", "lib/api/Client.ts"])
def test_auth_is_a_whole_word_and_names_that_merely_contain_it_have_no_floor(path):
    assert risk.floor([path]) == ("low", [])


def test_words_split_directories_and_the_file_stem():
    assert risk.words("app/OAuthHelper.kt") == {"app", "o", "auth", "helper"}
    assert risk.words("docs/author.md") == {"docs", "author"}


def test_plain_changes_have_no_floor():
    assert risk.floor(["docs/guide.md", "src/views/list.js", "tests/test_list.py"]) == ("low", [])
    assert risk.floor([]) == ("low", [])


@pytest.mark.parametrize("path", ["src/api/UserClient.swift", "app/routes/orders.py", "proto/orders.proto",
                                  "Sources/Networking/Upload.swift"])
def test_a_new_network_client_or_endpoint_is_high_only_when_added(path):
    assert risk.floor([path]) == ("low", [])
    assert risk.floor([path], added=[path]) == (
        "high", [{"source": "floor", "text": f"new network endpoint or client: {path}"}])


def test_low_risk_runs_its_panel_and_anything_else_runs_all_nine():
    assert risk.panel("low", ["security"]) == ["security"]
    assert risk.panel("medium", ["security"]) == list(risk.SPECIALISTS) == risk.panel(None, ["security"])
    assert len(risk.SPECIALISTS) == 9


def test_high_reasons_come_first_and_one_per_rule():
    level, reasons = risk.floor(["package.json", "a/migrations/1.py", "b/migrations/2.py"])
    assert level == "high"
    assert [r["text"] for r in reasons] == ["migrations and schema: a/migrations/1.py", "dependencies: package.json"]


def test_added_floors_raise_and_never_lower():
    assert risk.floor(["src/flags.py"], {"high": ["*flags*"]}) == \
        ("high", [{"source": "floor", "text": "added floor: src/flags.py"}])
    assert risk.floor(["deploy/run.sh"], {"medium": ["deploy/**"]})[0] == "high"


def test_a_diff_too_long_to_read_is_high():
    assert risk.floor(["a.md"] * 3001)[0] == "high"
    assert risk.floor(["a.md"], total=5000)[0] == "high"
    assert risk.floor(["a.md"] * 3000, total=3000)[0] == "low"


def test_the_model_can_raise_a_floor_and_never_lower_it():
    for floor_level, model_level in itertools.product(risk.LEVELS, risk.LEVELS):
        combined = risk.combine(floor_level, model_level)
        assert risk.rank(combined) == max(risk.rank(floor_level), risk.rank(model_level))
    assert risk.combine("low", "none") == "high"
    assert risk.combine("low", None) == "high"


def test_record_keeps_the_highest_level(engine, world):
    with engine.begin() as conn:
        first = [{"source": "floor", "text": "dependencies: package.json"}]
        assert risk.record(conn, world["run"], "medium", first) == "medium"
        assert risk.record(conn, world["run"], "low", [{"source": "model", "text": "small"}]) == "medium"
        model = [{"source": "model", "text": "touches retries"}]
        assert risk.record(conn, world["run"], "medium", model) == "medium"
        stored = conn.execute(select(runs.c.risk, runs.c.risk_reasons).where(runs.c.id == world["run"])).first()
        assert (stored.risk, stored.risk_reasons) == ("medium", first + model)
        assert risk.record(conn, world["run"], "bogus", []) == "high"
        with pytest.raises(LookupError):
            risk.record(conn, new_id(), "low", [])


class _RaisesFirst:
    """A connection that sets the run's risk to high just before record's update runs, as a concurrent writer would."""

    def __init__(self, conn, run_id):
        self.conn, self.run_id = conn, run_id

    def execute(self, statement):
        if isinstance(statement, Update):
            self.conn.execute(update(runs).where(runs.c.id == self.run_id).values(risk="high"))
        return self.conn.execute(statement)


def test_record_never_lowers_a_level_written_after_it_read(engine, world):
    with engine.begin() as conn:
        assert risk.record(_RaisesFirst(conn, world["run"]), world["run"], "low", []) == "high"
        assert conn.execute(select(runs.c.risk).where(runs.c.id == world["run"])).scalar() == "high"


def test_missing_risk_reads_as_high(engine, world):
    with engine.begin() as conn:
        assert risk.recorded(conn, world["run"]) == "high"
        assert risk.recorded(conn, None) == "high"
        assert risk.recorded(conn, new_id()) == "high"
        risk.record(conn, world["run"], "low", [])
        assert risk.recorded(conn, world["run"]) == "low"


def test_settings_are_checked_on_save(engine, world, clock):
    with engine.begin() as conn:
        saved = put_setting(conn, team_id=world["team"], project_id=world["project"], key=risk.FLOORS_KEY,
                            value={"high": [" *flags* ", "*flags*"]}, actor_id=None, now=clock(), ip=None)
        assert saved["value"] == {"high": ["*flags*"]}
        for key, value in ((risk.FLOORS_KEY, {"low": ["*.md"]}), (risk.FLOORS_KEY, {"high": "*.md"}),
                           (risk.FLOORS_KEY, ["*.md"]), (risk.PANEL_KEY, []), (risk.PANEL_KEY, ["Security"])):
            with pytest.raises(DefError) as caught:
                put_setting(conn, team_id=world["team"], project_id=world["project"], key=key, value=value,
                            actor_id=None, now=clock(), ip=None)
            assert caught.value.status == 400


def test_added_floors_merge_across_scopes_and_the_panel_takes_the_narrowest(engine, world, clock):
    with engine.begin() as conn:
        assert risk.extra_floors(conn, world["team"], world["project"]) == {"high": [], "medium": []}
        assert risk.low_risk_panel(conn, world["team"], world["project"]) == list(risk.DEFAULT_LOW_RISK_PANEL)
        put_setting(conn, team_id=None, key=risk.FLOORS_KEY, value={"high": ["*flags*"]}, actor_id=None,
                    now=clock(), ip=None)
        put_setting(conn, team_id=world["team"], project_id=world["project"], key=risk.FLOORS_KEY,
                    value={"high": ["*flags*", "*.lock"], "medium": ["*.yml"]}, actor_id=None, now=clock(), ip=None)
        put_setting(conn, team_id=None, key=risk.PANEL_KEY, value=["correctness"], actor_id=None, now=clock(),
                    ip=None)
        put_setting(conn, team_id=world["team"], key=risk.PANEL_KEY, value=["security", "correctness"],
                    actor_id=None, now=clock(), ip=None)
        assert risk.extra_floors(conn, world["team"], world["project"]) == \
            {"high": ["*flags*", "*.lock"], "medium": ["*.yml"]}
        assert risk.extra_floors(conn, world["team"], None) == {"high": ["*flags*"], "medium": []}
        assert risk.low_risk_panel(conn, world["team"], world["project"]) == ["security", "correctness"]
        assert risk.low_risk_panel(conn, None, None) == ["correctness"]
