"""The daily report: its build hook, its sections, its fingerprint, its file, and its admin-only routes."""
from __future__ import annotations

import os
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select

from armature.server.config import ReportsConfig, ServerConfig
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    audit,
    daily_reports,
    eval_arms,
    eval_experiments,
    findings,
    gh_pull_requests,
    gh_repos,
    llm_calls,
    run_artifacts,
    run_steps,
    runs,
    settings,
    versions,
    workflows,
)
from armature.server.insights import jobs, report, report_file
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    sign_in_as,
)
from tests.server.live_support import add_team

DAY = date(2026, 11, 16)
# 02:30 in Denver on the 17th, past the build margin.
NOW = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
SHA = "be3bbbe7494c06de9b458f1999556d2c8f1ba1d8"
TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
HOSTILE = '<script>alert("x")</script> & "quotes"'


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class Seed:
    def __init__(self, engine, team_id, project_id):
        self.engine, self.team, self.project = engine, team_id, project_id

    def workflow(self, name):
        workflow_id = new_id()
        with self.engine.begin() as conn:
            conn.execute(insert(workflows).values(id=workflow_id, name=name, kind="review", current_version=1,
                                                  created_at=NOW, updated_at=NOW))
        return workflow_id

    def run(self, workflow, status, started, minutes=10, cost=None, subject=None, review_round=None,
            trigger_ref=None):
        run_id = new_id()
        with self.engine.begin() as conn:
            workflow_id = conn.execute(select(workflows.c.id).where(workflows.c.name == workflow)).scalar()
            conn.execute(insert(runs).values(
                id=run_id, team_id=self.team, project_id=self.project, workflow_id=workflow_id,
                workflow_name=workflow, status=status, started_at=started,
                finished_at=started + timedelta(minutes=minutes) if status not in ("running", "queued") else None,
                cost_usd=Decimal(str(cost)) if cost is not None else None, subject=subject,
                review_round=review_round, trigger_ref=trigger_ref or {}))
        return run_id

    def step(self, run_id, step_id, error=None):
        with self.engine.begin() as conn:
            conn.execute(insert(run_steps).values(run_id=run_id, step_key=step_id, step_id=step_id,
                                                  status="failed" if error else "succeeded", error=error))

    def finding(self, run_id, key, lineage, blocking, decision=None):
        with self.engine.begin() as conn:
            conn.execute(insert(findings).values(id=new_id(), run_id=run_id, project_id=self.project,
                                                 finding_key=key, lineage_key=lineage, blocking=blocking,
                                                 decision=decision))

    def call(self, run_id, model, cost, seq):
        with self.engine.begin() as conn:
            conn.execute(insert(llm_calls).values(id=new_id(), team_id=self.team, run_id=run_id, seq=seq,
                                                  model_id=model, cost_usd=Decimal(str(cost)), input_tokens=100,
                                                  output_tokens=10, created_at=NOW))

    def arm(self, primary, arm_run):
        with self.engine.begin() as conn:
            experiment_id = new_id()
            conn.execute(insert(eval_experiments).values(
                id=experiment_id, team_id=self.team, specialist="security", variable="effort", baseline={},
                variant={"effort": "high"}, sample_rate=1.0, daily_budget_usd=25, max_concurrent_arms=2,
                status="running", min_prs=30, created_at=NOW))
            conn.execute(insert(eval_arms).values(id=new_id(), team_id=self.team, experiment_id=experiment_id,
                                                  primary_run_id=primary, arm_run_id=arm_run, head_sha="a" * 40,
                                                  label="variant", status="done", created_at=NOW))


@pytest.fixture
def host(tmp_path):
    log = tmp_path / "deploys.log"
    log.write_text("\n".join((
        "2026-11-15 23:59:59 deploy deploy/next (old) over older",
        "2026-11-16 10:00:00 deploy deploy/next (abc) over def",
        "2026-11-16 10:00:01 1/5 full suite on abc",
        f"2026-11-16 10:05:00 refused abc: token {TOKEN} leaked",
        f"2026-11-16 10:09:00 live: {SHA}",
        "2026-11-17 01:00:00 deploy deploy/next (next) over abc",
        "not a dated line",
    )) + "\n")
    sha = tmp_path / "live-sha"
    sha.write_text(SHA + "\n")
    return ReportsConfig(dir=str(tmp_path / "reports"), deploy_log=str(log), live_sha=str(sha))


@pytest.fixture
def seeded(sqlite_engine, keyring):
    team_id, project_id = add_team(sqlite_engine, lambda: NOW, "mobile")
    s = Seed(sqlite_engine, team_id, project_id)
    s.workflow("code-review")
    s.workflow("code-review-next")
    s.first = s.run("code-review", "succeeded", utc(16, 8), 20, 2.5, "acme/app#12", 1)
    s.second = s.run("code-review", "succeeded", utc(16, 10), 40, 4.0, "acme/app#12", 2)
    s.broken = s.run("code-review", "failed", utc(16, 12), 5, 0.5, "acme/app#13", 1)
    s.step(s.broken, "review-gh", f"JSON parse failed at offset 123 in run 9f3e2a1b with {TOKEN}\nTraceback")
    s.lost = s.run("code-review", "lost", utc(16, 13), 5, 0.25, "acme/app#14")
    s.step(s.lost, "jira-issue", "JSON parse failed at offset 456 in run 1234abcd with ghp_another0token")
    s.dry_empty = s.run("code-review-next", "succeeded", utc(16, 14), 3, None, "acme/app#12",
                        trigger_ref={"dry_run": True})
    s.dry_used = s.run("code-review-next", "succeeded", utc(16, 15), 3, None, "acme/app#13",
                       trigger_ref={"dry_run": True})
    with sqlite_engine.begin() as conn:
        conn.execute(insert(run_artifacts).values(run_id=s.dry_used, name="review.md", media_type="text/markdown",
                                                  content="x", size=1, created_at=NOW))
    # 23:00 in Denver on the 16th, still running at build time.
    s.open = s.run("code-review", "running", utc(17, 6), subject="acme/app#15")
    s.before = s.run("code-review", "succeeded", utc(16, 6, 30), 10, 100.0, "acme/app#9", 1)
    s.after = s.run("code-review", "succeeded", utc(17, 7, 30), 10, 100.0, "acme/app#10", 3)
    s.arm_run = s.run("code-review", "succeeded", utc(16, 9), 120, 50.0, "acme/app#12", 4)
    s.arm(s.first, s.arm_run)
    s.finding(s.first, "a1", "a1", True)
    s.finding(s.second, "b2", "a1", False)
    s.finding(s.second, "c3", "c3", False)
    s.finding(s.second, "d4", "d4", True, decision="drop")
    s.finding(s.arm_run, "e5", "e5", True)
    s.call(s.first, "claude-opus-5-5", 1.0, 1)
    s.call(s.first, "claude-opus-5-5", 1.5, 2)
    s.call(s.second, "claude-haiku-5-5", 0.25, 1)
    s.call(s.arm_run, "claude-opus-5-5", 50.0, 1)
    s.call(s.after, "claude-opus-5-5", 9.0, 1)
    author, _ = add_user(sqlite_engine, keyring, lambda: NOW, "mei")
    with sqlite_engine.begin() as conn:
        repo_row = new_id()
        conn.execute(insert(gh_repos).values(id=repo_row, team_id=team_id, gh_repo_id=1, full_name="acme/app",
                                             created_at=NOW, updated_at=NOW))
        conn.execute(insert(gh_pull_requests).values(id=new_id(), team_id=team_id, repo_row_id=repo_row, gh_pr_id=12,
                                                     number=12, title=HOSTILE, author_gh_id=7, author_login="mei",
                                                     state="open", created_at_gh=NOW, gh_updated_at=NOW,
                                                     created_at=NOW, updated_at=NOW))
        workflow_id = conn.execute(select(workflows.c.id).where(workflows.c.name == "code-review")).scalar()
        for version, created, note in ((2, utc(16, 15), "Tighten the prompt. Authorization: Bearer abc.def"),
                                       (3, utc(17, 8), "Too late")):
            conn.execute(insert(versions).values(id=new_id(), object_kind="workflow", object_id=workflow_id,
                                                 version=version, body="x", author_id=author, created_at=created,
                                                 note=note))
        for action, detail, at in (
                ("trigger.update", {"changed": ["config", "enabled"], "config": ["cron"]}, utc(16, 16)),
                ("setting.set", {"key": "display.timezone", "scope": INSTANCE_SCOPE}, utc(16, 17)),
                ("webhook.rule_update", {"webhook_id": "w1", "changed": ["enabled"]}, utc(16, 18)),
                ("trigger.run", {"changed": ["ignored"]}, utc(16, 19)),
                ("setting.set", {"key": "insights.label_daily_cap"}, utc(17, 8))):
            conn.execute(insert(audit).values(id=new_id(), at=at, actor_id=author, team_id=team_id, action=action,
                                              target_kind="trigger", target_id="t1", detail=detail))
    return s


def _build(engine, cfg, day=DAY):
    with engine.connect() as conn:
        return report.build(conn, day, cfg)


def test_the_report_covers_the_local_day_in_every_section(sqlite_engine, seeded, host):
    data = _build(sqlite_engine, host)
    assert set(data) == {"version", "day", "zone", "open_runs", "headline", "findings", "learned", "changes",
                         "fingerprint"}
    assert (data["version"], data["day"], data["zone"], data["open_runs"]) == (1, "2026-11-16", "America/Denver", 1)
    head = data["headline"]
    # Six runs started on the 16th in Denver, and the open one. The arm, the 15th's, and the 17th's don't count.
    assert head["runs"] == {"value": 7, "previous": 1, "avg_7d": round(1 / 7, 2)}
    assert head["cost_usd"]["value"] == pytest.approx(7.25)
    assert head["cost_usd"]["previous"] == pytest.approx(100.0)
    assert head["failed"]["value"] == 2 and head["finished"] == 6
    assert [(w["workflow"], w["runs"]) for w in head["by_workflow"]] == [("code-review", 5), ("code-review-next", 2)]
    assert head["series"]["days"][-1] == "2026-11-16" and len(head["series"]["days"]) == 8
    assert head["models"] == [
        {"model": "claude-opus-5-5", "calls": 2, "cost_usd": 2.5, "input_tokens": 200, "output_tokens": 20},
        {"model": "claude-haiku-5-5", "calls": 1, "cost_usd": 0.25, "input_tokens": 100, "output_tokens": 10}]

    found = data["findings"]
    [error] = found["errors"]
    assert error["pattern"] == "JSON parse failed at offset # in run # with [redacted]"
    assert (error["count"], error["runs"], error["workflows"]) == (2, 2, ["code-review"])
    assert found["failing"] == [{"workflow": "code-review", "runs": 5, "failed": 2, "canceled": 0}]
    assert found["slowest"][0]["run_id"] == seeded.second and found["slowest"][0]["seconds"] == 2400
    assert [r["run_id"] for r in found["costliest"]][:2] == [seeded.second, seeded.first]
    assert found["dry"] == {"runs": 2, "empty": 1, "by_workflow": [{"workflow": "code-review-next", "runs": 1}]}
    review = found["churn"]
    assert (review["prs"], review["multi_round"], review["max_rounds"]) == (2, 1, 2)
    assert (review["findings"], review["blocking"], review["non_blocking"], review["repeats"], review["late"]) == \
        (3, 1, 2, 1, 1)
    top = review["top"][0]
    assert (top["subject"], top["rounds"], top["title"]) == ("acme/app#12", 2, HOSTILE)
    assert top["link"] == {"repo": "acme/app", "number": 12, "url": "https://github.com/acme/app/pull/12"}

    texts = " ".join(item["text"] for item in data["learned"])
    assert "7 runs" in texts and "$7.25 spent" in texts and "2 of 6 finished runs failed" in texts
    assert "1 of 2 dry runs produced no finding" in texts and "1 of 3 findings were late catches" in texts

    changes = data["changes"]
    assert [line["line"] for line in changes["deploys"]["lines"]] == [
        "deploy deploy/next (abc) over def", "refused abc: token [redacted] leaked", f"live: {SHA}"]
    assert changes["deploys"]["live_sha"] == SHA
    [version] = changes["versions"]
    assert (version["name"], version["version"], version["author"]) == ("code-review", 2, "mei")
    assert TOKEN not in str(data) and "abc.def" not in version["note"]
    assert [(c["action"], c["keys"]) for c in changes["config"]] == [
        ("trigger.update", ["config", "config.cron", "enabled"]), ("setting.set", ["display.timezone"]),
        ("webhook.rule_update", ["enabled"])]
    assert changes["fingerprint"] == {"baseline": True, "files": 0, "added": [], "removed": [], "changed": []}


def test_an_empty_day_still_has_every_key(sqlite_engine, host):
    data = _build(sqlite_engine, ReportsConfig(dir=host.dir))
    assert data["headline"]["runs"] == {"value": 0, "previous": 0, "avg_7d": 0.0}
    assert data["findings"]["errors"] == [] and data["findings"]["churn"]["top"] == []
    assert data["learned"] == []
    assert data["changes"]["deploys"] == {"lines": [], "live_sha": None}
    assert "Nothing stood out." in report_file.render(data)


def test_the_day_follows_the_zone_across_a_dst_change(sqlite_engine, seeded, host):
    # Denver falls back on November 1, so that day runs from 06:00 UTC to 07:00 UTC the next day.
    seeded.run("code-review", "succeeded", datetime(2026, 11, 2, 6, 30, tzinfo=UTC), 5, 1.0)
    seeded.run("code-review", "succeeded", datetime(2026, 11, 1, 5, 30, tzinfo=UTC), 5, 1.0)
    assert _build(sqlite_engine, host, date(2026, 11, 1))["headline"]["runs"]["value"] == 1
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(key="display.timezone", scope=INSTANCE_SCOPE, value="UTC"))
    data = _build(sqlite_engine, host)
    # In UTC the 15th's late run moves into the day and the open one moves out.
    assert (data["zone"], data["headline"]["runs"]["value"], data["open_runs"]) == ("UTC", 7, 0)


def test_the_fingerprint_hashes_only_configured_roots_and_skips_symlinks_and_secrets(sqlite_engine, tmp_path):
    root = tmp_path / "ops"
    (root / "secrets").mkdir(parents=True)
    (root / "secrets" / "key").write_text("secret")
    (root / "ops.env").write_text("TOKEN=1")
    (root / "flows").mkdir()
    (root / "flows" / "a.yml").write_text("a")
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours")
    os.symlink(outside, root / "flows" / "link.yml")
    os.symlink(tmp_path, root / "loop")
    cfg = ReportsConfig(dir=str(tmp_path / "reports"), roots=[str(root), str(tmp_path / "missing")])
    first = _build(sqlite_engine, cfg)
    assert list(first["fingerprint"]) == ["ops/flows/a.yml"]
    assert first["fingerprint"]["ops/flows/a.yml"] == report._digest(root / "flows" / "a.yml")
    assert first["changes"]["fingerprint"]["baseline"] is True
    # The diff is against the newest earlier report, which needn't be the day before.
    with sqlite_engine.begin() as conn:
        conn.execute(insert(daily_reports).values(id=new_id(), day=date(2026, 11, 13), data=first, created_at=NOW))
        conn.execute(insert(daily_reports).values(id=new_id(), day=date(2026, 11, 20), data={}, created_at=NOW))
    (root / "flows" / "a.yml").write_text("changed")
    (root / "flows" / "b.yml").write_text("b")
    diff = _build(sqlite_engine, cfg)["changes"]["fingerprint"]
    assert diff == {"baseline": False, "files": 2, "added": ["ops/flows/b.yml"], "removed": [],
                    "changed": ["ops/flows/a.yml"]}


def test_fingerprint_roots_come_only_from_the_config_file():
    with pytest.raises(ValidationError):
        ReportsConfig(dir="/tmp/r", extra="no")
    with pytest.raises(ValidationError):
        ReportsConfig(roots=["/a/ops", "/b/ops"])
    assert ServerConfig().reports.dir == "~/armature-reports"


def test_the_file_escapes_every_value_and_links_only_to_github(sqlite_engine, seeded, host):
    data = _build(sqlite_engine, host)
    data["findings"]["churn"]["top"].append({"subject": "javascript:alert(1)", "rounds": 1, "runs": 1, "title": None})
    page = report_file.render(data)
    assert "<script>" not in page and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;quotes&quot;" in page
    assert '<a href="https://github.com/acme/app/pull/12">acme/app#12</a>' in page
    assert 'href="javascript' not in page and page.count("<a href=") == 2
    assert "<style>" in page and "prefers-color-scheme: dark" in page and "max-width: 640px" in page
    # A row from an older version, missing keys, still renders.
    assert "What Armature learned" in report_file.render({"day": "2026-11-10"})


def test_the_file_is_0600_in_a_0700_folder_and_an_unsafe_folder_is_refused(tmp_path):
    where = tmp_path / "reports"
    path = report_file.write(str(where), DAY, "<p>x</p>")
    assert path == where / "2026-11-16.html" and path.read_text() == "<p>x</p>"
    assert (os.stat(where).st_mode & 0o777, os.stat(path).st_mode & 0o777) == (0o700, 0o600)
    assert [p.name for p in where.iterdir()] == ["2026-11-16.html"]
    report_file.write(str(where), DAY, "<p>y</p>")
    assert path.read_text() == "<p>y</p>"
    link = tmp_path / "link"
    os.symlink(where, link)
    with pytest.raises(report_file.Unsafe):
        report_file.write(str(link), DAY, "x")
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    os.chmod(open_dir, 0o755)
    with pytest.raises(report_file.Unsafe):
        report_file.write(str(open_dir), DAY, "x")
    assert list(open_dir.iterdir()) == []


def _stored(engine):
    with engine.connect() as conn:
        return conn.execute(select(daily_reports.c.day, daily_reports.c.data)).all()


def test_the_report_waits_for_the_margin_then_stores_one_row_and_its_file(sqlite_engine, seeded, host):
    early = datetime(2026, 11, 17, 8, 59, tzinfo=UTC)
    assert jobs.daily_report(sqlite_engine, host, early) is None
    assert _stored(sqlite_engine) == []
    assert jobs.daily_report(sqlite_engine, host, NOW) == DAY
    assert jobs.daily_report(sqlite_engine, host, NOW + timedelta(hours=3)) is None
    [row] = _stored(sqlite_engine)
    assert row.day == DAY and row.data["headline"]["runs"]["value"] == 7
    page = (report_file.folder(host.dir) / "2026-11-16.html").read_text()
    assert "What Armature learned on Monday, November 16, 2026" in page and TOKEN not in page


def test_a_failed_build_retries_on_the_next_tick(sqlite_engine, keyring, seeded, host, monkeypatch):
    real = report.build
    calls = []

    def flaky(conn, day, cfg):
        calls.append(day)
        if len(calls) == 1:
            raise RuntimeError("database went away")
        return real(conn, day, cfg)
    monkeypatch.setattr(report, "build", flaky)
    daemon = jobs.Jobs(sqlite_engine, keyring, clock=Clock(NOW), reports=host)
    daemon.tick()
    assert _stored(sqlite_engine) == []
    daemon.tick()
    daemon.tick()
    assert [r.day for r in _stored(sqlite_engine)] == [DAY] and calls == [DAY, DAY]


def test_two_builds_at_once_store_one_row(sqlite_engine, seeded, host, monkeypatch):
    real = report.build
    other: list = []

    def racing(conn, day, cfg):
        data = real(conn, day, cfg)
        if not other:
            # Another server finishes the same day while this one is still building.
            other.append(None)
            thread = threading.Thread(target=lambda: other.append(jobs.daily_report(sqlite_engine, host, NOW)))
            thread.start()
            thread.join()
        return data
    monkeypatch.setattr(report, "build", racing)
    assert jobs.daily_report(sqlite_engine, host, NOW) is None
    assert other == [None, DAY]
    assert len(_stored(sqlite_engine)) == 1


def _signed_in(engine, keyring, clock, user_id):
    client = make_client(engine, keyring, clock)
    sign_in_as(client, engine, keyring, clock, user_id)
    return client


def test_only_an_admin_reads_the_reports(sqlite_engine, keyring, seeded, host):
    clock = Clock(NOW)
    assert jobs.daily_report(sqlite_engine, host, NOW) == DAY
    admin = _signed_in(sqlite_engine, keyring, clock, add_user(sqlite_engine, keyring, clock, "root",
                                                               is_admin=True)[0])
    listed = admin.get("/api/insights/daily-reports").json()
    assert listed["timezone"] == "America/Denver" and listed["builds_after_hour"] == 2
    assert [d["day"] for d in listed["days"]] == ["2026-11-16"]
    body = admin.get("/api/insights/daily-reports/2026-11-16").json()
    assert body["day"] == "2026-11-16" and body["data"]["headline"]["runs"]["value"] == 7
    assert admin.get("/api/insights/daily-reports/2026-11-15").status_code == 404
    assert admin.get("/api/insights/daily-reports/yesterday").status_code == 400
    lead_id = add_user(sqlite_engine, keyring, clock, "lead")[0]
    add_membership(sqlite_engine, clock, lead_id, "team", seeded.team, "team_lead")
    lead = _signed_in(sqlite_engine, keyring, clock, lead_id)
    assert lead.get("/api/insights/daily-reports").status_code == 403
    assert lead.get("/api/insights/daily-reports/2026-11-16").status_code == 403


def test_days_missed_after_the_newest_report_are_built_oldest_first(sqlite_engine, seeded, host):
    assert jobs.daily_report(sqlite_engine, host, NOW - timedelta(days=3)) == DAY - timedelta(days=3)
    built = [jobs.daily_report(sqlite_engine, host, NOW) for _ in range(4)]
    assert built == [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY, None]
