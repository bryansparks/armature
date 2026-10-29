"""The code-review starters end to end: the engine runs them with stub agent sessions, and each built-in tool step
calls the real broker against a fake GitHub."""
from __future__ import annotations

import json
import subprocess

import pytest
from sqlalchemy import insert, select, update

from armature.nodes.agent import AgentNode
from armature.nodes.gate import CHECKPOINT_FILE, GATE_DIR_ENV, GATE_FILE, GateSuspended
from armature.nodes.script import ScriptNode
from armature.packs import tool as tool_cli
from armature.runtime.engine import Harness
from armature.server import broker, defs, rbac, risk, starters
from armature.server.db.schema import findings as finding_rows
from armature.server.db.schema import gates as gate_rows
from armature.server.db.schema import review_marks, runs, settings
from armature.spec.calls import MappingCallResolver, ResolvedCall, load_callee
from tests.server.tool_support import (  # noqa: F401
    HEAD,
    NUMBER,
    REPO,
    app,
    calls,
    clock,
    dns,
    pem,
    start_run,
    use,
    vendor,
    world,
)

SPECS = {(name, 1): starters.body(name) for name in starters.KINDS}
PINS = {name: 1 for name in starters.KINDS}
BLOCKING = {"path": "src/a.swift", "line": 3, "severity": "high", "blocking": True, "body": "It leaks.",
            "claim": "the handle leaks", "raised_by": ["correctness"]}
INPUTS = {"repo": f"https://github.com/{REPO}", "slug": REPO, "number": NUMBER, "ref": HEAD, "branch": "feature"}


def test_the_starters_validate_and_call_only_each_other():
    admin = rbac.Access(is_admin=True, teams={}, projects={}, project_teams={})
    for name in starters.KINDS:
        validation = defs.validate_workflow(starters.body(name), actor=admin, previous_body=None,
                                            catalog=broker.builtin_catalog(),
                                            call_resolver=MappingCallResolver(SPECS, PINS))
        assert validation.ok, (name, validation.to_dict())


def test_seeding_adds_the_ten_review_roles_once_and_keeps_an_edited_one(sqlite_engine, clock):
    from armature.server import worker
    with sqlite_engine.begin() as conn:
        defs._insert(conn, defs.DOCUMENT, defs.INSTANCE, "security", "role", "---\nname: security\n---\nOurs.",
                     None, None, clock())
        assert starters.seed_roles(conn, clock()) == [k for k in starters.ROLES if k != "security"]
        assert starters.seed_roles(conn, clock()) == []
        found = worker.bundle_roles(conn, "team", None, starters.ROLES)
    assert found["security"] == "Ours."
    assert found["test_adequacy"].startswith("You are reviewing a pull request")
    assert "Your role is whether the tests actually verify" in found["test_adequacy"]
    assert set(found) == set(starters.ROLES) and set(starters.ROLES) - {"team_lead"} == set(risk.SPECIALISTS)


class Flow:
    """Plays the agent sessions and routes each built-in tool step through the broker as the run's step."""

    def __init__(self, app, keyring, clock, run, vendor, tree):
        self.app, self.keyring, self.clock, self.run, self.vendor, self.tree = app, keyring, clock, run, vendor, tree
        self.level = "low"
        self.verdicts = {1: "approve", 2: "approve"}
        self.specialists: list[list[str]] = []
        self.sessions: list[str] = []
        self.tools: list[str] = []

    async def agent(self, node, context):
        stage = node._stage.id
        self.sessions.append(stage)
        if stage == "rate":
            ref = context["ref"]
            return {"level": self.level, "reasons": ["stub"],
                    "commit": ref if len(ref) == 40 else self.vendor.branches[ref]}
        if stage in risk.SPECIALISTS:
            assert node._stage.agent.role == stage
            self.specialists[-1].append(stage)
            return {"findings": []}
        if stage == "team_lead":
            verdict = self.verdicts[context.get("round") or 1]
            return {"verdict": verdict, "summary": "s", "comments": [] if verdict == "approve" else [BLOCKING],
                    "decisions": [{"claim": BLOCKING["claim"], "path": BLOCKING["path"], "raised_by": ["correctness"],
                                   "decision": "drop" if verdict == "approve" else "block", "reason": "r"}]}
        if stage == "fix":
            (self.tree / "src.txt").write_text("fixed\n")
            built = (self.tree / ".armature" / "build").read_text().endswith("exit 0\n")
            if built:
                git(self.tree, "commit", "-qam", "Fix the leak")
            return {"fixed": [{"claim": BLOCKING["claim"], "path": "src/a.swift", "reason": "closed it"}],
                    "built": built, "worktree": str(self.tree), "commit": git(self.tree, "rev-parse", "HEAD")}
        raise AssertionError(stage)

    async def script(self, node, original, context):
        cmd = node._adapter.cmd or ""
        if node._adapter.pack_dir is None or not cmd.startswith("armature tool "):
            return await original(node, context)
        name, params = cmd.split()[2], json.loads(node._env["ARMATURE_WITH"])
        if name == "review.marked":
            self.specialists.append([])
        self.tools.append(name)
        token = broker.encode(self.keyring, broker.Grant(self.run.id, node._step_id, (name,),
                                                         self.clock() + broker.GRANT_MAX))
        if name == "github.push":
            # The runner's own push, with the server's gate behind its call and a local remote for GitHub.
            self.push_token = token
            out = tool_cli.push(params)
            if out.get("pushed"):
                self.vendor.pr["head"]["sha"] = out["commit"]
            return out
        response = use(self.app, token, name, params)
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, name, params):
        response = use(self.app, self.push_token, name, params)
        assert response.status_code == 200, response.text
        return response.json()


def git(where, *args):
    return subprocess.run(["git", "-C", str(where), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def flow(app, sqlite_engine, keyring, clock, world, vendor, tmp_path, monkeypatch):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    tree = tmp_path / "worktree"
    tree.mkdir()
    git(tree, "init", "-q")
    git(tree, "config", "user.email", "agent@test")
    git(tree, "config", "user.name", "agent")
    (tree / ".armature").mkdir()
    (tree / ".armature" / "build").write_text("#!/bin/sh\nexit 0\n")
    (tree / ".armature" / "build").chmod(0o755)
    (tree / "src.txt").write_text("leaky\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "base")
    work = tmp_path / "run"
    work.mkdir()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    monkeypatch.setattr(tool_cli, "REMOTE_PROTOCOL", "file")
    monkeypatch.setattr(tool_cli, "_url", lambda repo: str(remote))
    monkeypatch.chdir(work)
    found = Flow(app, keyring, clock, start_run(sqlite_engine, keyring, clock, world, risk=None), vendor, tree)
    found.remote = remote
    original = ScriptNode.execute

    async def script(node, context):
        return await found.script(node, original, context)

    async def agent(node, context):
        return await found.agent(node, context)

    monkeypatch.setattr(tool_cli, "call", found.call)
    monkeypatch.setattr(ScriptNode, "execute", script)
    monkeypatch.setattr(AgentNode, "execute", agent)
    return found


async def run_flow(tmp_path, name, inputs):
    spec = load_callee(ResolvedCall(name, 1, starters.body(name)))
    harness = Harness(spec=spec, session_dir=tmp_path / "session", call_resolver=MappingCallResolver(SPECS, PINS))
    return await harness.run(inputs)


def passes(tools):
    """The tool steps in order, without review.decisions, which runs in the same wave as the post."""
    return [t for t in tools if t != "review.decisions"]


def findings_of(engine):
    with engine.connect() as conn:
        return conn.execute(select(finding_rows).order_by(finding_rows.c.created_at, finding_rows.c.step_id)).all()


def stored(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).first()


def marks(engine):
    with engine.connect() as conn:
        return conn.execute(select(review_marks.c.head_sha, review_marks.c.run_id)).all()


async def test_a_low_risk_review_runs_the_small_panel_and_marks_the_approved_head(flow, sqlite_engine, tmp_path,
                                                                                  vendor):
    result = await run_flow(tmp_path, "code-review", INPUTS)
    assert sorted(flow.specialists[0]) == sorted(risk.DEFAULT_LOW_RISK_PANEL)
    assert passes(flow.tools) == ["review.marked", "review.risk", "github.post-review", "review.mark"]
    assert flow.tools.count("review.decisions") == 1
    assert [(f.step_id, f.decision, f.reason) for f in findings_of(sqlite_engine)] == [("decisions", "drop", "r")]
    assert result["first"]["risk"] == "low" and result["first"]["verdict"] == "approve"
    assert result["fix"] == {"_skipped": True}
    run = stored(sqlite_engine, flow.run.id)
    assert (run.risk, run.head_sha, run.outcome, run.outcome_source) == ("low", HEAD, "approved", "server")
    assert marks(sqlite_engine) == [(HEAD, flow.run.id)]
    assert [c["conclusion"] for c in vendor.check_runs] == ["success"]


async def test_a_marked_head_is_a_noop(flow, sqlite_engine, keyring, clock, world, tmp_path):
    await run_flow(tmp_path, "code-review", INPUTS)
    flow.tools.clear()
    flow.sessions.clear()
    again = flow.run = start_run(sqlite_engine, keyring, clock, world, risk=None)
    result = await run_flow(tmp_path, "code-review", INPUTS)
    assert flow.tools == ["review.marked"] and flow.sessions == []
    assert result["first"]["marked"] is True
    assert stored(sqlite_engine, again.id).outcome == "noop"


async def test_a_high_risk_review_runs_all_nine_and_never_starts_a_fix(flow, sqlite_engine, tmp_path, vendor):
    vendor.files = [{"filename": "db/migrations/0002.sql", "status": "added"}]
    flow.verdicts[1] = "comment"
    result = await run_flow(tmp_path, "code-review", {**INPUTS, "auto_fix": True})
    assert sorted(flow.specialists[0]) == sorted(risk.SPECIALISTS)
    assert result["pushable"] == {"pushable": False, "reason": "the run's risk is high"}
    assert result["fix"] == result["push"] == result["second"] == {"_skipped": True}
    assert "fix" not in flow.sessions and "github.push" not in flow.tools
    assert vendor.pr["head"]["sha"] == HEAD
    run = stored(sqlite_engine, flow.run.id)
    assert (run.risk, run.outcome) == ("high", "recommended")
    assert marks(sqlite_engine) == []


async def test_auto_fix_pushes_once_and_reviews_the_new_head(flow, sqlite_engine, tmp_path,
                                                                               vendor):
    flow.verdicts[1] = "comment"
    result = await run_flow(tmp_path, "code-review", {**INPUTS, "auto_fix": True})
    pushed = result["push"]["commit"]
    assert result["push"]["pushed"] is True and pushed != HEAD
    assert result["fixed"] == {"recorded": 1}
    assert passes(flow.tools) == ["review.marked", "review.risk", "github.post-review", "review.pushable",
                                  "github.push",
                                  "review.marked", "review.risk", "github.post-review", "review.mark"]
    fixed = [f for f in findings_of(sqlite_engine) if f.step_id == "fixed"]
    assert [(f.decision, f.fix_sha, f.reason) for f in fixed] == [("fixed", pushed, "closed it")]
    assert flow.sessions.count("fix") == 1
    assert result["second"]["head"] == pushed and result["second"]["verdict"] == "approve"
    assert git(flow.remote, "rev-parse", "refs/heads/feature") == pushed == result["fix"]["commit"]
    [approved] = [c for c in calls(sqlite_engine) if c.tool == "github.push"]
    assert approved.detail["commit"] == pushed
    run = stored(sqlite_engine, flow.run.id)
    assert (run.head_sha, run.outcome) == (pushed, "pushed")
    assert marks(sqlite_engine) == [(pushed, flow.run.id)]
    assert [c.outcome for c in calls(sqlite_engine) if c.tool == "github.push"] == ["approved"]


async def test_a_round_2_review_never_fixes(flow, tmp_path):
    flow.verdicts[2] = "comment"
    result = await run_flow(tmp_path, "code-review", {**INPUTS, "auto_fix": True, "round": 2})
    assert result["fix"] == {"_skipped": True} and result["push"] == {"_skipped": True}
    assert "github.push" not in flow.tools and "fix" not in flow.sessions


async def test_a_failed_build_pushes_nothing(flow, sqlite_engine, tmp_path):
    (flow.tree / ".armature" / "build").write_text("#!/bin/sh\nexit 1\n")
    git(flow.tree, "commit", "-qam", "break the build")
    flow.verdicts[1] = "comment"
    result = await run_flow(tmp_path, "code-review", {**INPUTS, "auto_fix": True})
    assert result["fix"]["built"] is False and result["push"] == {"_skipped": True}
    assert stored(sqlite_engine, flow.run.id).outcome == "recommended"


@pytest.mark.parametrize("change", [lambda v: v.pr["head"]["repo"].update(full_name="mallory/App"),
                                    lambda v: v.pr["user"].update(login="stranger")])
async def test_a_fork_or_a_disallowed_author_never_starts_a_fix(flow, sqlite_engine, tmp_path, vendor, change):
    with sqlite_engine.begin() as conn:
        conn.execute(update(settings).where(settings.c.key == "github.push_authors").values(value=["jstutz4"]))
    change(vendor)
    flow.verdicts[1] = "comment"
    result = await run_flow(tmp_path, "code-review", {**INPUTS, "auto_fix": True})
    assert result["pushable"]["pushable"] is False
    assert result["fix"] == result["push"] == {"_skipped": True} and "fix" not in flow.sessions


DEV = {"repo": f"https://github.com/{REPO}", "slug": REPO, "ref": "feature", "base": "main", "title": "Add it"}


async def test_development_opens_reviews_and_marks_its_pull_request(flow, sqlite_engine, tmp_path, vendor):
    vendor.opened = NUMBER
    result = await run_flow(tmp_path, "development-review", DEV)
    assert result["approval"] == {"_skipped": True}
    assert result["open"]["number"] == NUMBER
    assert flow.tools[:2] == ["review.risk", "github.open-pr"]
    run = stored(sqlite_engine, flow.run.id)
    assert run.outcome == "pr_opened"
    assert marks(sqlite_engine) == [(HEAD, flow.run.id)]


async def test_high_risk_development_pauses_at_the_gate_before_its_pull_request(flow, sqlite_engine, tmp_path,
                                                                                vendor, monkeypatch, world, clock):
    vendor.files = [{"filename": "Sources/Auth/Session.swift", "status": "modified"}]
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    monkeypatch.setenv(GATE_DIR_ENV, str(gate_dir))
    with pytest.raises(GateSuspended):
        await run_flow(tmp_path, "development-review", DEV)
    request = json.loads((gate_dir / GATE_FILE).read_text())
    assert request["step_id"] == "approval"
    assert request["present"].startswith("Branch feature of Acme/App is high risk.")
    assert flow.tools == ["review.risk"] and stored(sqlite_engine, flow.run.id).risk == "high"

    checkpoint = {**request["checkpoint"], "approval": {"approved": False, "feedback": "not yet"}}
    (gate_dir / CHECKPOINT_FILE).write_text(json.dumps(checkpoint))
    (gate_dir / GATE_FILE).unlink()
    result = await run_flow(tmp_path, "development-review", DEV)
    assert result["open"] == {"_skipped": True} and result["review"] == {"_skipped": True}
    assert flow.tools == ["review.risk"]

    checkpoint["approval"] = {"approved": True, "feedback": None}
    (gate_dir / CHECKPOINT_FILE).write_text(json.dumps(checkpoint))
    vendor.opened = NUMBER
    clock.advance(seconds=1)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gate_rows).values(id="g-1", run_id=flow.run.id, team_id=world.team, step_id="approval",
                                              step_key="approval", state="approved", nonce="n" * 64,
                                              created_at=clock(), expires_at=clock(), decided_at=clock()))
    result = await run_flow(tmp_path, "development-review", DEV)
    assert result["open"]["number"] == NUMBER and "github.open-pr" in flow.tools
