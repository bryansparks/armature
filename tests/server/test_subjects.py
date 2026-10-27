"""The subject list and page: grouping, status, finding history, scoping, and subjects with '#' and '/'."""
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import pytest
from sqlalchemy import insert, select, update

from armature.server import subjects
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    findings,
    projects,
    run_artifacts,
    runs,
    settings,
    teams,
    workflows,
)
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    sign_in_as,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
PR = "acme/app#12"
OTHER_PR = "acme/app#13"
ISSUE = "PROJ-7"
FOREIGN = "rival/app#1"
KINDS = {"code-review": "review", "pr-fix": "fix", "pr-merged": "merge", "dev-work": "work"}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 23, hour, minute, tzinfo=UTC)


def _run(conn, team_id, workflow, started, subject, *, status="succeeded", cost=None, summary=None,
         trigger_ref=None, project_id=None):
    run_id = new_id()
    workflow_id = conn.execute(select(workflows.c.id).where(workflows.c.name == workflow)).scalar()
    conn.execute(insert(runs).values(id=run_id, team_id=team_id, project_id=project_id, workflow_id=workflow_id,
                                     workflow_name=workflow, status=status, started_at=started, finished_at=started,
                                     cost_usd=Decimal(str(cost)) if cost is not None else None, subject=subject,
                                     summary=summary, trigger_ref=trigger_ref))
    return run_id


def _finding(conn, run_id, key, disposition, *, title=None, path=None, line=None, created=NOW):
    conn.execute(insert(findings).values(id=new_id(), run_id=run_id, finding_key=key, disposition=disposition,
                                         title=title, path=path, line=line, created_at=created))


@pytest.fixture
def seeded(engine, keyring):
    clock = Clock(NOW)
    with engine.begin() as conn:
        own, other = new_id(), new_id()
        conn.execute(insert(teams).values(id=own, name="own", created_at=NOW))
        conn.execute(insert(teams).values(id=other, name="other", created_at=NOW))
        for name, kind in KINDS.items():
            conn.execute(insert(workflows).values(id=new_id(), name=name, kind=kind, current_version=1,
                                                  created_at=NOW, updated_at=NOW))
        project = new_id()
        conn.execute(insert(projects).values(id=project, team_id=own, name="app", created_at=NOW))
        ref = {"repo": "acme/app", "number": 12}
        first = _run(conn, own, "code-review", at(1), PR, cost=1.5, project_id=project,
                     summary={"title": "Old title", "verdict": "Changes Recommended",
                              "links": [{"label": "Pull request", "url": "https://github.com/acme/app/pull/12"},
                                        {"label": "Review", "url": "https://github.com/acme/app/pull/12#r1"}]},
                     trigger_ref={**ref, "head": "a" * 40})
        fix = _run(conn, own, "pr-fix", at(2), PR, cost=0.5,
                   trigger_ref={**ref, "mode": "pr", "fix": {"outcome": "pushed", "applied": 3, "deep": {"x": 1}}})
        second = _run(conn, own, "code-review", at(3), PR, cost=1.0,
                      summary={"title": "New title", "verdict": "Approval Recommended",
                               "links": [{"label": "Pull request", "url": "https://github.com/acme/app/pull/12"},
                                         {"label": "Review", "url": "https://github.com/acme/app/pull/12#r2"},
                                         {"label": "Bad", "url": "javascript:alert(1)"}]},
                      trigger_ref={**ref, "head": "b" * 40})
        _run(conn, own, "pr-merged", at(4), PR, cost=9,
             trigger_ref={**ref, "merge": {"merged_by": "dev", "transitioned": True}})
        _run(conn, own, "code-review", at(5), OTHER_PR, status="running", summary={"title": "Second PR"})
        _run(conn, own, "dev-work", at(6), ISSUE, status="failed", cost=2, summary={"title": "Add the field"})
        _run(conn, own, "code-review", at(7), None)
        _run(conn, other, "code-review", at(8), FOREIGN, summary={"title": "Rival"})
        _finding(conn, first, "k1", "keep", title="Leak", path="a.py", line=4)
        _finding(conn, second, "k1", "resolved")
        _finding(conn, second, "k2", "keep", title="New issue")
        conn.execute(insert(run_artifacts).values(run_id=first, name="review.md", media_type="text/markdown",
                                                  content="# r", size=3, created_at=NOW))
    viewer, _ = add_user(engine, keyring, clock, "vee")
    add_membership(engine, clock, viewer, "team", own, "viewer")
    project_viewer, _ = add_user(engine, keyring, clock, "pat")
    add_membership(engine, clock, project_viewer, "project", project, "viewer")
    admin, _ = add_user(engine, keyring, clock, "root", is_admin=True)
    return {"clock": clock, "own": own, "other": other, "viewer": viewer, "admin": admin,
            "project_viewer": project_viewer, "first": first, "second": second, "fix": fix}


def _client(engine, keyring, seeded, who):
    client = make_client(engine, keyring, seeded["clock"])
    sign_in_as(client, engine, keyring, seeded["clock"], seeded[who])
    return client


def _path(subject):
    return f"/api/subjects/{quote(subject, safe='')}"


def test_list_groups_runs_by_subject_newest_first(engine, keyring, seeded):
    body = _client(engine, keyring, seeded, "viewer").get("/api/subjects").json()
    rows = {row["subject"]: row for row in body["subjects"]}
    assert [row["subject"] for row in body["subjects"]] == [ISSUE, OTHER_PR, PR]
    assert body["next_before"] is None
    pr = rows[PR]
    assert pr["title"] == "New title" and pr["status"] == "merged"
    assert pr["latest_verdict"] == "Approval Recommended"
    # pr-merged is hidden by default, so it stays out of the counts and cost but still marks the status.
    assert (pr["runs"], pr["reviews"], pr["fixes"]) == (3, 2, 1)
    assert pr["cost_usd"] == pytest.approx(3.0)
    assert pr["repo"] == "acme/app" and pr["team_id"] == seeded["own"]
    assert rows[OTHER_PR]["status"] == "running"
    assert rows[ISSUE]["status"] == "failed" and rows[ISSUE]["repo"] is None


def test_status_falls_back_to_the_latest_review_verdict(engine, keyring, seeded):
    with engine.begin() as conn:
        _run(conn, seeded["own"], "code-review", at(9), OTHER_PR, summary={"verdict": "Not approved"})
    rows = {r["subject"]: r for r in _client(engine, keyring, seeded, "viewer").get("/api/subjects").json()["subjects"]}
    assert rows[OTHER_PR]["status"] == "Not approved"
    assert rows[OTHER_PR]["title"] == "Second PR"


def test_list_pages_with_a_cursor(engine, keyring, seeded):
    client = _client(engine, keyring, seeded, "viewer")
    first = client.get("/api/subjects", params={"limit": 2}).json()
    assert [r["subject"] for r in first["subjects"]] == [ISSUE, OTHER_PR]
    rest = client.get("/api/subjects", params={"limit": 2, "before": first["next_before"]}).json()
    assert [r["subject"] for r in rest["subjects"]] == [PR]
    assert rest["next_before"] is None
    assert client.get("/api/subjects", params={"before": "nonsense"}).status_code == 400


def test_search_matches_subject_or_latest_title(engine, keyring, seeded):
    client = _client(engine, keyring, seeded, "viewer")
    by_title = client.get("/api/subjects", params={"q": "NEW TITLE"}).json()["subjects"]
    assert [r["subject"] for r in by_title] == [PR]
    by_subject = client.get("/api/subjects", params={"q": "app#13"}).json()["subjects"]
    assert [r["subject"] for r in by_subject] == [OTHER_PR]
    # An older run's title matched the coarse filter, but the row's title is the latest one.
    assert client.get("/api/subjects", params={"q": "old title"}).json()["subjects"] == []
    assert client.get("/api/subjects", params={"q": "100%"}).json()["subjects"] == []


def test_search_fills_a_page_across_batches(engine, keyring, seeded):
    with engine.begin() as conn:
        for minute in range(5):
            _run(conn, seeded["own"], "code-review", at(10, minute), f"acme/app#{100 + minute}",
                 summary={"title": "Noise", "verdict": "title-ish"})
    client = _client(engine, keyring, seeded, "viewer")
    body = client.get("/api/subjects", params={"q": "title", "limit": 1}).json()
    assert [r["subject"] for r in body["subjects"]] == [PR]


def test_list_is_scoped_to_what_the_viewer_sees(engine, keyring, seeded):
    viewer = _client(engine, keyring, seeded, "viewer")
    assert FOREIGN not in {r["subject"] for r in viewer.get("/api/subjects").json()["subjects"]}
    assert viewer.get("/api/subjects", params={"team": seeded["other"]}).status_code == 404
    assert viewer.get("/api/subjects", params={"team": "missing"}).status_code == 404
    project_viewer = _client(engine, keyring, seeded, "project_viewer")
    rows = project_viewer.get("/api/subjects").json()["subjects"]
    assert [(r["subject"], r["runs"]) for r in rows] == [(PR, 1)]
    admin = _client(engine, keyring, seeded, "admin")
    narrowed = admin.get("/api/subjects", params={"team": seeded["other"]}).json()["subjects"]
    assert [r["subject"] for r in narrowed] == [FOREIGN]


def test_run_list_rows_carry_the_pull_request_state(engine, keyring, seeded):
    rows = {r["id"]: r for r in _client(engine, keyring, seeded, "viewer").get("/api/runs").json()["runs"]}
    fix = rows[seeded["fix"]]
    assert fix["title"] is None and fix["outcome"] == "pushed"
    assert fix["pr"] == {"title": "New title", "verdict": "Approval Recommended", "merged": True}
    issue = next(r for r in rows.values() if r["subject"] == ISSUE)
    assert issue["pr"] is None and issue["outcome"] is None
    assert FOREIGN not in {r["subject"] for r in rows.values()}
    scoped = _client(engine, keyring, seeded, "project_viewer").get("/api/runs").json()["runs"]
    assert [(r["id"], r["pr"]) for r in scoped] == [
        (seeded["first"], {"title": "Old title", "verdict": "Changes Recommended", "merged": False})]


def test_hidden_workflows_change_counts_never_access(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(insert(settings).values(scope=INSTANCE_SCOPE, key="display.workflow_labels",
                                             value={"pr-fix": {"label": "", "color": "", "hidden": True}},
                                             updated_at=NOW))
        conn.execute(insert(settings).values(scope=INSTANCE_SCOPE, key="display.show_cost", value=False,
                                             updated_at=NOW))
    client = _client(engine, keyring, seeded, "viewer")
    row = next(r for r in client.get("/api/subjects").json()["subjects"] if r["subject"] == PR)
    assert (row["runs"], row["fixes"], row["cost_usd"]) == (3, 0, None)
    detail = client.get(_path(PR)).json()["subject"]
    assert [r["workflow"] for r in detail["runs"]] == ["code-review", "pr-fix", "code-review", "pr-merged"]
    assert all(r["cost_usd"] is None for r in detail["runs"])


def test_a_renamed_workflow_keeps_its_kind(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(workflows).where(workflows.c.name == "code-review").values(name="pr-check"))
        conn.execute(update(workflows).where(workflows.c.name == "pr-merged").values(name="pr-landed"))
        conn.execute(update(workflows).where(workflows.c.name == "pr-fix").values(kind="other"))
    client = _client(engine, keyring, seeded, "viewer")
    row = next(r for r in client.get("/api/subjects").json()["subjects"] if r["subject"] == PR)
    assert (row["reviews"], row["fixes"], row["latest_verdict"]) == (2, 0, "Approval Recommended")
    detail = client.get(_path(PR)).json()["subject"]
    assert detail["status"] == "merged"
    assert [(r["workflow"], r["kind"]) for r in detail["runs"]] == [
        ("pr-check", "review"), ("pr-fix", "other"), ("pr-check", "review"), ("pr-landed", "merge")]


def test_a_run_with_no_workflow_row_is_other(engine, keyring, seeded):
    with engine.begin() as conn:
        _run(conn, seeded["own"], "jira-work", at(9), "PROJ-8")
    detail = _client(engine, keyring, seeded, "viewer").get(_path("PROJ-8")).json()["subject"]
    assert [(r["workflow"], r["kind"]) for r in detail["runs"]] == [("jira-work", "other")]


def test_detail_has_runs_links_and_finding_history(engine, keyring, seeded):
    detail = _client(engine, keyring, seeded, "viewer").get(_path(PR)).json()["subject"]
    assert (detail["subject"], detail["title"], detail["status"]) == (PR, "New title", "merged")
    assert (detail["repo"], detail["number"]) == ("acme/app", 12)
    assert detail["links"] == [{"label": "Pull request", "url": "https://github.com/acme/app/pull/12"},
                               {"label": "Review", "url": "https://github.com/acme/app/pull/12#r2"}]
    kinds = [(r["kind"], r["workflow_label"]) for r in detail["runs"]]
    assert kinds == [("review", "code-review"), ("fix", "pr-fix"), ("review", "code-review"),
                     ("merge", "pr-merged")]
    first, fix, second, merge = detail["runs"]
    assert first["head"] == "a" * 12 and first["artifacts"] == ["review.md"] and second["artifacts"] == []
    assert first["verdict"] == "Changes Recommended" and second["cost_usd"] == pytest.approx(1.0)
    assert fix["mode"] == "pr" and fix["fix"] == {"outcome": "pushed", "applied": 3}
    assert merge["merge"] == {"merged_by": "dev", "transitioned": True}
    by_key = {f["finding_key"]: f for f in detail["findings"]}
    assert [f["finding_key"] for f in detail["findings"]] == ["k2", "k1"]
    k1 = by_key["k1"]
    assert (k1["title"], k1["path"], k1["line"]) == ("Leak", "a.py", 4)
    assert [(h["run_id"], h["disposition"]) for h in k1["history"]] == [(seeded["first"], "keep"),
                                                                          (seeded["second"], "resolved")]
    assert k1["current_disposition"] == "resolved"
    assert by_key["k2"]["current_disposition"] == "keep"


def test_detail_is_404_without_a_visible_run(engine, keyring, seeded):
    viewer = _client(engine, keyring, seeded, "viewer")
    assert viewer.get(_path(FOREIGN)).status_code == 404
    assert viewer.get(_path("acme/app#999")).status_code == 404
    assert _client(engine, keyring, seeded, "admin").get(_path(FOREIGN)).status_code == 200
    project_viewer = _client(engine, keyring, seeded, "project_viewer")
    detail = project_viewer.get(_path(PR)).json()["subject"]
    assert [r["id"] for r in detail["runs"]] == [seeded["first"]]
    assert [f["finding_key"] for f in detail["findings"]] == ["k1"]


def test_subject_with_hash_and_slashes_round_trips(engine, keyring, seeded):
    odd = "group/sub/repo#5"
    with engine.begin() as conn:
        _run(conn, seeded["own"], "code-review", at(11), odd, summary={"title": "Nested"})
    client = _client(engine, keyring, seeded, "viewer")
    detail = client.get(_path(odd)).json()["subject"]
    assert detail["subject"] == odd and detail["repo"] == "group/sub/repo" and detail["number"] == 5
    # The UI sends slashes literally, because Apache refuses %2F in a path.
    assert client.get("/api/subjects/group/sub/repo%235").json()["subject"]["subject"] == odd
    assert client.get(_path(ISSUE)).json()["subject"]["number"] is None


def test_merged_links_skip_unsafe_and_repeated_entries():
    class Run:
        def __init__(self, summary):
            self.summary = summary

    ordered = [Run({"post_url": "https://x/old"}), Run({"links": "not a list", "post_url": "http://x/insecure"}),
               Run({"links": [{"label": "PR", "url": "https://x/pr"}, {"label": "PR", "url": "https://x/other"},
                              {"url": "https://x/no-label"}]})]
    assert subjects.merged_links(ordered) == [{"label": "PR", "url": "https://x/pr"},
                                              {"label": "Posted result", "url": "https://x/old"}]
