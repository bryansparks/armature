"""`armature mission validate` CLI (slice 1)."""
import re
from pathlib import Path
from typer.testing import CliRunner

from armature.cli import app

runner = CliRunner()


def plain(s: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


VALID = """\
name: m
objective: Ship the thing.
work:
  - id: a
    title: Unit A
    workflow: registered-worker
"""

INVALID = """\
name: m
objective: Ship the thing.
work:
  - id: a
    title: Unit A
    workflow: registered-worker
    requires: [missing-unit]
"""


def test_mission_validate_ok(tmp_path):
    p = tmp_path / "m.mission.yml"
    p.write_text(VALID)
    result = runner.invoke(app, ["mission", "validate", str(p)])
    out = plain(result.output)
    assert result.exit_code == 0
    assert "'m' is valid" in out
    assert "1 work unit" in out


def test_mission_validate_reports_codes(tmp_path):
    p = tmp_path / "m.mission.yml"
    p.write_text(INVALID)
    result = runner.invoke(app, ["mission", "validate", str(p)])
    out = plain(result.output)
    assert result.exit_code == 1
    assert "[UNKNOWN_WORK_UNIT]" in out


def test_mission_validate_missing_file(tmp_path):
    result = runner.invoke(app, ["mission", "validate", str(tmp_path / "nope.yml")])
    assert result.exit_code == 1
    assert "not found" in plain(result.output)


def test_mission_validate_warnings_do_not_fail(tmp_path):
    p = tmp_path / "m.mission.yml"
    p.write_text(VALID)
    result = runner.invoke(app, ["mission", "validate", str(p)])
    out = plain(result.output)
    assert result.exit_code == 0          # WORKFLOW_UNVERIFIED_NAME is a warning
    assert "[WORKFLOW_UNVERIFIED_NAME]" in out

EXAMPLE = Path(__file__).parents[2] / "examples" / "missions" / "campaign-pretzel.mission.yml"


def test_shipped_example_mission_valid():
    assert EXAMPLE.exists(), f"missing shipped example: {EXAMPLE}"
    result = runner.invoke(app, ["mission", "validate", str(EXAMPLE)])
    out = plain(result.output)
    assert result.exit_code == 0, out
    assert "is valid" in out


# ── Slice 2: mission run + mission status ────────────────────────────────────

from armature.state.work import LocalWorkStore, WorkUnitState

OK_SPEC = """\
name: ok-flow
adapters:
  echo:
    name: echo
    type: script
    cmd: "echo '{\\"ok\\": true}'"
    parse: json
stages:
  - id: work
    adapter: echo
    depends_on: []
"""

FAIL_SPEC = """\
name: fail-flow
adapters:
  boom:
    name: boom
    type: script
    cmd: "exit 3"
    parse: json
stages:
  - id: work
    adapter: boom
    depends_on: []
"""

MISSION_TEXT = """\
name: m
objective: Ship it.
work:
  - id: a
    title: A
    workflow: wf.yml
  - id: b
    title: B
    workflow: wf.yml
    requires: [a]
    max_attempts: 2
"""


def _write_pair(tmp_path, mission_text, workflow_text):
    (tmp_path / "wf.yml").write_text(workflow_text)
    p = tmp_path / "m.mission.yml"
    p.write_text(mission_text)
    return p


def test_mission_run_success_reaches_done(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    out = plain(r.output)
    assert r.exit_code == 0, out
    assert "done" in out
    store = LocalWorkStore(tmp_path / "store")
    assert store.load("m", "a").state == WorkUnitState.DONE
    assert store.load("m", "a").attempts == 1


def test_mission_run_failure_lands_retry_pending_then_failed(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, FAIL_SPEC)
    s = tmp_path / "store"
    r1 = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(s)])
    assert r1.exit_code == 1
    store = LocalWorkStore(s)
    rec = store.load("m", "a")
    assert rec.state == WorkUnitState.RETRY_PENDING      # attempts 1 < max 2
    r2 = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(s)])
    assert r2.exit_code == 1
    assert store.load("m", "a").state == WorkUnitState.FAILED   # ceiling reached


def test_mission_run_refuses_unready_requires(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "b", "--store", str(tmp_path / "store")])
    out = plain(r.output)
    assert r.exit_code == 1
    assert "requires" in out or "a" in out
    store = LocalWorkStore(tmp_path / "store")
    assert store.load("m", "b").attempts == 0           # no attempt consumed


def test_mission_run_refuses_unknown_unit(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "nope", "--store", str(tmp_path / "store")])
    assert r.exit_code == 1
    assert "unknown unit" in plain(r.output)


def test_mission_run_injects_objective_into_context(tmp_path):
    spec = OK_SPEC.replace("name: ok-flow", "name: ok-flow\nmission_source: work_unit")
    p = _write_pair(tmp_path, MISSION_TEXT, spec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 0, plain(r.output)
    # injection itself is asserted at engine level (Task 4); here: run completed
    # and the record's last_job_id was stamped
    assert LocalWorkStore(tmp_path / "store").load("m", "a").last_job_id


def test_mission_run_does_not_inject_without_opt_in(tmp_path):
    """Fix M-1: the record is injected only when the spec opts in via
    mission_source: work_unit — otherwise it would be an ungovernable
    context key (never: [work_unit] is rejected as UNKNOWN_CONTEXT_SOURCE)."""
    probe_spec = """\
name: probe-flow
adapters:
  ctx_probe:
    name: ctx_probe
    type: script
    cmd: "if echo \\"$ARMATURE_CONTEXT\\" | grep -q work_unit; then exit 1; else echo '{\\"ok\\": true}'; fi"
    parse: json
stages:
  - id: work
    adapter: ctx_probe
    depends_on: []
"""
    assert "mission_source" not in probe_spec
    p = _write_pair(tmp_path, MISSION_TEXT, probe_spec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 0, plain(r.output)
    assert LocalWorkStore(tmp_path / "store").load("m", "a").state == WorkUnitState.DONE


def test_mission_status_renders_states_and_transitions(tmp_path):
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    s = tmp_path / "store"
    runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(s)])
    r = runner.invoke(app, ["mission", "status", str(p), "--store", str(s)])
    out = plain(r.output)
    assert r.exit_code == 0, out
    assert "a" in out and "done" in out
    assert "b" in out and "pending" in out
    assert "recent transitions" in out


def test_shipped_example_status_renders(tmp_path):
    r = runner.invoke(app, ["mission", "status", str(EXAMPLE), "--store", str(tmp_path)])
    assert r.exit_code == 0, plain(r.output)
    assert "hero-headlines" in plain(r.output)
    assert "brand-approval" in plain(r.output)
    assert "pending" in plain(r.output)
    out = plain(r.output)
    # strengthened (passed immediately off Task 5's command): per-unit rows
    # carry posture and the requires column
    assert "delegated" in out and "human-led" in out
    hero_row = next(l for l in out.splitlines() if l.startswith("  hero-headlines"))
    assert "brand-approval" in hero_row          # requires column
    assert "0/2" in hero_row                     # attempts/max column


STATUS_ALIGN_TEXT = """\
name: m
objective: Ship it.
work:
  - id: a
    title: A
    workflow: wf.yml
  - id: a-much-longer-unit-identifier
    title: B
    workflow: wf.yml
"""


def test_mission_status_columns_align(tmp_path):
    """M-4: column widths are computed from the rendered cells — rows stay
    aligned across unit-id lengths."""
    p = _write_pair(tmp_path, STATUS_ALIGN_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "status", str(p), "--store", str(tmp_path / "store")])
    out = plain(r.output)
    rows = [l for l in out.splitlines() if l.startswith("  a")]
    assert len(rows) == 2
    offsets = {l.index("pending") for l in rows}
    assert len(offsets) == 1          # state column aligned despite id lengths


def test_mission_status_lists_dynamic_follow_on_units(tmp_path):
    """Closure-seeded units (absent from the doc) render in the table with a
    seeded-by marker."""
    from armature.spec.mission import load_mission
    from armature.state.closure import FollowOnUnit
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    mission = load_mission(p)
    store.ensure_unit(mission, mission.work[0])
    store.seed_follow_on("m", FollowOnUnit(id="dyn", title="Dynamic", workflow="wf.yml"))
    r = runner.invoke(app, ["mission", "status", str(p), "--store", str(store_dir)])
    out = plain(r.output)
    assert r.exit_code == 0, out
    # pin the TABLE row, not the transitions footer (the seeding transition's
    # reason text mentions closure regardless)
    dyn_row = next(l for l in out.splitlines() if l.startswith("  dyn"))
    assert "closure" in dyn_row          # seeded-by marker on the unit row


# ── Slice 3: closure-driven terminal states, budget gates, metering ───────────

CLOSURE_SPEC = """\
name: closure-flow
adapters:
  echo:
    name: echo
    type: script
    cmd: "echo '{\\"reason\\": \\"%(reason)s\\"%(extra)s}'"
    parse: json
stages:
  - id: work
    adapter: echo
    depends_on: []
  - id: final
    adapter: echo
    output_mode: guided_json
    output_schema:
      type: object
      required: [reason]
      properties:
        reason:
          type: string
          enum: [done_no_follow_on, handed_off, blocked_on, escalation]
        follow_on:
          type: array
          items:
            type: object
            required: [id, title, workflow]
            properties:
              id: {type: string}
              title: {type: string}
              workflow: {type: string}
    depends_on: [work]
closure:
  stage: final
"""

MISSION_BUDGET_TEXT = MISSION_TEXT.replace(
    "objective: Ship it.", "objective: Ship it.\nbudget_usd: 5.0")


def test_mission_run_success_with_closure_hands_off(tmp_path):
    """Review Focus #2: a successful run's closure is authoritative for the
    resting state — handed_off seeds its follow-ons as pending work."""
    spec = CLOSURE_SPEC % {"reason": "handed_off",
        "extra": ', \\"follow_on\\": [{\\"id\\": \\"polish\\", \\"title\\": \\"P\\", \\"workflow\\": \\"wf.yml\\"}]'}
    p = _write_pair(tmp_path, MISSION_TEXT, spec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 0, plain(r.output)
    store = LocalWorkStore(tmp_path / "store")
    assert store.load("m", "a").state == WorkUnitState.HANDED_OFF
    assert store.load("m", "polish").state == WorkUnitState.PENDING


def test_mission_run_closure_seeded_follow_on_is_runnable(tmp_path):
    """Final-review I1: a closure-seeded unit (absent from the doc) is runnable
    by the local executor — the plan's goal is end-to-end self-coordination,
    so the follow-on chain must not dead-end at 'unknown unit'. The store
    record is the source of truth; the workflow ref resolves doc-dir-first,
    same convention as doc units."""
    from armature.spec.mission import load_mission
    from armature.state.closure import FollowOnUnit
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    mission = load_mission(p)
    store.ensure_unit(mission, mission.work[0])
    store.seed_follow_on("m", FollowOnUnit(id="polish", title="Polish",
                                           workflow="wf.yml"))
    r = runner.invoke(app, ["mission", "run", str(p), "polish", "--store", str(store_dir)])
    assert r.exit_code == 0, plain(r.output)
    assert store.load("m", "polish").state == WorkUnitState.DONE
    assert store.load("m", "polish").attempts == 1


def test_mission_run_failure_with_closure_spec_lands_retry_pending(tmp_path):
    """Review Focus #1: the closure stage never ran (the run failed first) —
    no closure, slice-2 failure path."""
    spec = CLOSURE_SPEC % {"reason": "done_no_follow_on", "extra": ""}
    spec = spec.replace('cmd: "echo', 'cmd: "exit 3 && echo')
    p = _write_pair(tmp_path, MISSION_TEXT, spec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 1
    assert LocalWorkStore(tmp_path / "store").load("m", "a").state == \
        WorkUnitState.RETRY_PENDING


def test_mission_run_malformed_closure_fails_the_unit(tmp_path):
    """A malformed closure record is loud (design §3): the unit fails and may
    retry — it never silently lands done."""
    spec = CLOSURE_SPEC % {"reason": "not-a-closure-reason", "extra": ""}
    p = _write_pair(tmp_path, MISSION_TEXT, spec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 1
    assert "closure" in plain(r.output)
    assert LocalWorkStore(tmp_path / "store").load("m", "a").state == \
        WorkUnitState.RETRY_PENDING


def test_mission_run_blocked_on_reenters_when_requires_done(tmp_path):
    """Review Focus #4a: a blocked_on unit whose requires are now done may
    start again (design §2.3 — unblock, then run)."""
    from armature.spec.mission import load_mission
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(store_dir)])
    assert r.exit_code == 0, plain(r.output)              # a is done
    mission = load_mission(p)
    store.ensure_unit(mission, mission.work[1])
    store.apply("m", "b", WorkUnitState.IN_PROGRESS, reason="start")
    store.apply("m", "b", WorkUnitState.BLOCKED_ON, reason="waiting on a")
    r = runner.invoke(app, ["mission", "run", str(p), "b", "--store", str(store_dir)])
    assert r.exit_code == 0, plain(r.output)
    assert store.load("m", "b").state == WorkUnitState.DONE


def test_mission_run_blocked_on_refused_while_requires_pending(tmp_path):
    from armature.spec.mission import load_mission
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    mission = load_mission(p)
    store.ensure_unit(mission, mission.work[1])
    store.apply("m", "b", WorkUnitState.IN_PROGRESS, reason="start")
    store.apply("m", "b", WorkUnitState.BLOCKED_ON, reason="waiting on a")
    r = runner.invoke(app, ["mission", "run", str(p), "b", "--store", str(store_dir)])
    assert r.exit_code == 1
    assert store.load("m", "b").attempts == 0           # no attempt consumed
    assert store.load("m", "b").state == WorkUnitState.BLOCKED_ON


def test_mission_run_blocked_on_checks_record_requires_too(tmp_path):
    """blocked_on closures rewrite the RECORD's requires (design §3) — re-entry
    checks those dynamic blockers, not just the spec's static ones."""
    from armature.spec.mission import load_mission
    from armature.state.closure import FollowOnUnit
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    mission = load_mission(p)
    store.ensure_unit(mission, mission.work[0])
    store.apply("m", "a", WorkUnitState.IN_PROGRESS, reason="start")
    store.apply("m", "a", WorkUnitState.DONE, reason="done")
    store.ensure_unit(mission, mission.work[1])
    store.apply("m", "b", WorkUnitState.IN_PROGRESS, reason="start")
    store.apply("m", "b", WorkUnitState.BLOCKED_ON, reason="blocked")
    rec = store.load("m", "b")
    rec.requires = ["a", "dyn"]
    store.save(rec)
    store.seed_follow_on("m", FollowOnUnit(id="dyn", title="Dyn", workflow="wf.yml"))
    r = runner.invoke(app, ["mission", "run", str(p), "b", "--store", str(store_dir)])
    assert r.exit_code == 1
    assert store.load("m", "b").attempts == 0
    assert store.load("m", "b").state == WorkUnitState.BLOCKED_ON


def test_mission_run_refuses_when_unit_budget_spent(tmp_path):
    """Review Focus #3: budget refusal happens BEFORE attempts += 1."""
    from armature.spec.mission import load_mission
    p = _write_pair(tmp_path, MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    mission = load_mission(p)
    unit = next(u for u in mission.work if u.id == "a")
    rec = store.ensure_unit(mission, unit)
    rec.max_budget_usd = 1.0
    rec.spent_usd = 1.0
    store.save(rec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(store_dir)])
    assert r.exit_code == 1
    assert "budget" in plain(r.output)
    assert store.load("m", "a").attempts == 0           # no attempt consumed


def test_mission_run_refuses_when_mission_budget_spent(tmp_path):
    from armature.spec.mission import load_mission
    p = _write_pair(tmp_path, MISSION_BUDGET_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    store = LocalWorkStore(store_dir)
    rec = store.ensure_unit(load_mission(p), load_mission(p).work[0])
    rec.spent_usd = 6.0
    store.save(rec)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(store_dir)])
    assert r.exit_code == 1
    assert "budget" in plain(r.output)
    assert store.load("m", "a").attempts == 0


LLM_SPEC = """\
name: llm-flow
model_tiers:
  small: {provider: mock, model: m}
role_type_defaults:
  worker: small
stages:
  - id: work
    role: {name: W, type: worker, description: do it}
    depends_on: []
  - id: work2
    role: {name: W, type: worker, description: more}
    depends_on: [work]
"""


class _CostedLLM:
    """Fake LLM node: each call costs $0.01; with fail_after set, the call
    after that many successes raises."""
    calls = 0
    fail_after: int | None = None

    def __init__(self, **kwargs):
        pass

    def _resolve_model(self) -> str:
        return "fake"

    async def execute(self, context):
        _CostedLLM.calls += 1
        if _CostedLLM.fail_after is not None and _CostedLLM.calls > _CostedLLM.fail_after:
            raise RuntimeError("stage 2 boom")
        return {"content": "ok", "_input_tokens": 1, "_output_tokens": 1,
                "_cost_usd": 0.01, "_escalation_count": 0, "_tools_called": []}


def test_mission_run_meters_spent_usd(tmp_path, monkeypatch):
    from armature.runtime import engine as engine_mod
    _CostedLLM.calls = 0
    _CostedLLM.fail_after = None
    monkeypatch.setattr(engine_mod, "LLMNode", _CostedLLM)
    p = _write_pair(tmp_path, MISSION_TEXT, LLM_SPEC)
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(tmp_path / "store")])
    assert r.exit_code == 0, plain(r.output)
    rec = LocalWorkStore(tmp_path / "store").load("m", "a")
    assert abs(rec.spent_usd - 0.02) < 1e-9       # both stages metered


def test_mission_run_meters_spent_usd_on_failure(tmp_path, monkeypatch):
    """A failed attempt still meters what it spent before crashing (design
    §2.3 — spent_usd is run-observed, on success AND failure)."""
    from armature.runtime import engine as engine_mod
    _CostedLLM.calls = 0
    _CostedLLM.fail_after = 1
    monkeypatch.setattr(engine_mod, "LLMNode", _CostedLLM)
    p = _write_pair(tmp_path, MISSION_TEXT, LLM_SPEC)
    store_dir = tmp_path / "store"
    r = runner.invoke(app, ["mission", "run", str(p), "a", "--store", str(store_dir)])
    assert r.exit_code == 1
    rec = LocalWorkStore(store_dir).load("m", "a")
    assert rec.state == WorkUnitState.RETRY_PENDING
    assert abs(rec.spent_usd - 0.01) < 1e-9


# ── Slice 3: mission advance — pure readiness for executors ──────────────────

ADVANCE_MISSION_TEXT = MISSION_TEXT.replace(
    "objective: Ship it.", "objective: Ship it.\nposture: delegated")


def test_mission_advance_renders_table(tmp_path):
    p = _write_pair(tmp_path, ADVANCE_MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "advance", str(p), "--store", str(tmp_path / "store")])
    out = plain(r.output)
    assert r.exit_code == 0, out
    assert "launchable" in out and "a" in out


def test_mission_advance_json_is_machine_readable(tmp_path):
    import json as _json
    p = _write_pair(tmp_path, ADVANCE_MISSION_TEXT, OK_SPEC)
    r = runner.invoke(app, ["mission", "advance", str(p), "--store", str(tmp_path / "store"),
                            "--json"])
    payload = _json.loads(plain(r.output))
    assert any(x["unit_id"] == "a" and x["launchable"] for x in payload)


def test_mission_advance_never_executes(tmp_path):
    """Design §5: advance returns decisions only — no attempt is consumed,
    no state moves."""
    p = _write_pair(tmp_path, ADVANCE_MISSION_TEXT, OK_SPEC)
    store_dir = tmp_path / "store"
    r = runner.invoke(app, ["mission", "advance", str(p), "--store", str(store_dir)])
    assert r.exit_code == 0, plain(r.output)
    rec = LocalWorkStore(store_dir).load("m", "a")
    assert rec.attempts == 0
    assert rec.state == WorkUnitState.PENDING
