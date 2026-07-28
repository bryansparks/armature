"""Tests for the ``TuneRunner`` closed-loop facade (``armature.tune.runner``).

The runner orchestrates three injectable factories (harness / improve / optimize)
so tests run without real LLM calls. The stall detector is exercised end-to-end
through the seeded ``ImprovementStore`` history — the same substrate production
uses.
"""
import itertools
from datetime import datetime, timezone
from pathlib import Path

import pytest

from armature.optimizer.runner import OptimizationResult
from armature.state.improvement_store import ImprovementRecord, ImprovementStore
from armature.state.traces import TraceStore, TraceRecord
from armature.spec.models import HarnessSpec, ModelTiers, ModelTierConfig
from armature.synthesis.improve import ImprovementReport
from armature.tune.runner import TuneRunner


# ── helpers ───────────────────────────────────────────────────────────────────

def _spec():
    return HarnessSpec(
        name="wf",
        stages=[],
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
    )


def _report(*, hqs_before=0.60, needs_improvement=True, applied=False, n_traces=5,
            escalated_oscillation=False, proposed_spec=None):
    return ImprovementReport(
        workflow_name="wf", spec_path=Path("/tmp/wf.yaml"), n_traces=n_traces,
        hqs_before=hqs_before, needs_improvement=needs_improvement, applied=applied,
        diagnostics=[], proposed_spec=proposed_spec,
        escalated_oscillation=escalated_oscillation,
    )


def _rec(*, record_id="r0", escalated_oscillation=False, triggered_by_drift=False,
          applied=False, hqs_before=None, latency_risk=0.0, missed_predictions=None,
          unexpected_regressions=None, verified_fixes=None, ts=None):
    return ImprovementRecord(
        record_id=record_id, workflow_stem="wf", source="improve",
        timestamp=ts or datetime.now(timezone.utc).isoformat(),
        escalated_oscillation=escalated_oscillation,
        triggered_by_drift=triggered_by_drift, applied=applied,
        hqs_before=hqs_before, latency_risk=latency_risk,
        missed_predictions=missed_predictions or [],
        unexpected_regressions=unexpected_regressions or [],
        verified_fixes=verified_fixes or [],
    )


class _FakeHarness:
    """Fresh harness per call. Writes ``calls_per_iter`` trace rows so budget
    accounting works; returns a canned result. Mints its own run_id."""

    def __init__(self, traces_db, idx, calls_per_iter=0, in_tok=10, out_tok=5):
        self._traces_db = traces_db
        self._run_id = f"fake-{idx}"
        self._calls_per_iter = calls_per_iter
        self._in_tok = in_tok
        self._out_tok = out_tok

    async def run(self, inputs):
        if self._calls_per_iter:
            store = TraceStore(self._traces_db)
            await store.init()
            ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()
            for i in range(self._calls_per_iter):
                await store.record(TraceRecord(
                    run_id=self._run_id, workflow_name="wf", stage_id=f"s{i}",
                    role_type="worker", model="fake",
                    input_tokens=self._in_tok, output_tokens=self._out_tok,
                    timestamp=ts,
                ))
        return {"ok": True}


def _harness_factory(traces_db, calls_per_iter=0):
    counter = itertools.count(1)
    def make(spec_path):
        return _FakeHarness(traces_db, idx=next(counter), calls_per_iter=calls_per_iter)
    return make


class _FakeImprove:
    """Returns canned reports in order. Shared across iterations so the index
    advances (the real runner constructs a fresh runner each iteration but reads
    the same evolving store; here we model the evolving outcome directly)."""

    def __init__(self, reports):
        self._reports = list(reports)
        self._i = 0

    async def analyze(self):
        r = self._reports[self._i % len(self._reports)]
        self._i += 1
        return r


def _improve_factory(reports):
    fake = _FakeImprove(reports)
    return (lambda spec_path: fake), fake


class _FakeOptimize:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def optimize(self):
        self.calls += 1
        return self.result


def _optimize_factory(result):
    fake = _FakeOptimize(result)
    return (lambda spec_path, trace_db: fake), fake


def _opt_result(*, accepted=True, proposed_diff="- old\n+ new", score=0.8):
    return OptimizationResult(
        accepted=accepted, proposed_diff=proposed_diff, rationale="rationale",
        confidence=0.9, score=score, feedback="feedback",
    )


async def _seed_stalled_oscillation(db_path):
    """Two source=improve records with escalated_oscillation → detect_stall fires."""
    store = ImprovementStore(db_path)
    await store.init()
    await store.record(_rec(record_id="s1", escalated_oscillation=True,
                           triggered_by_drift=True, hqs_before=0.60,
                           ts="2026-01-01T00:00:01+00:00"))
    await store.record(_rec(record_id="s2", escalated_oscillation=True,
                           triggered_by_drift=True, hqs_before=0.61,
                           ts="2026-01-01T00:00:02+00:00"))


@pytest.fixture
def apply_recorder(monkeypatch):
    """Replace OptimizerRunner.apply_diff with a call recorder."""
    calls = []
    def _fake_apply(spec_path, diff_text):
        calls.append((str(spec_path), diff_text))
        return True, "applied"
    import armature.tune.runner as tune_runner
    monkeypatch.setattr(tune_runner.OptimizerRunner, "apply_diff", staticmethod(_fake_apply))
    return calls


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_tune_converges_on_healthy_hqs(tmp_path):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"
    imp_factory, _ = _improve_factory([_report(hqs_before=0.95, needs_improvement=False)])
    opt_factory, opt = _optimize_factory(_opt_result())
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, harness_factory=_harness_factory(db),
        improve_factory=imp_factory, optimize_factory=opt_factory,
        max_iterations=5,
    )
    res = await runner.run()
    assert res.stop_reason == "converged"
    assert len(res.iterations) == 1
    assert opt.calls == 0
    assert res.final_hqs == pytest.approx(0.95)


async def test_tune_escalates_on_stall_then_converges(tmp_path, apply_recorder):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"
    await _seed_stalled_oscillation(idb)
    # iter1: stalled (hqs 0.60) → escalate; iter2: optimize's diff moved HQS up → converged
    imp_factory, _ = _improve_factory([
        _report(hqs_before=0.60, needs_improvement=True),
        _report(hqs_before=0.95, needs_improvement=False),
    ])
    opt_factory, opt = _optimize_factory(_opt_result(accepted=True))
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, max_escalations=2, auto_apply=True,
        harness_factory=_harness_factory(db), improve_factory=imp_factory,
        optimize_factory=opt_factory, max_iterations=5,
    )
    res = await runner.run()
    assert res.stop_reason == "converged"
    assert opt.calls == 1
    assert res.escalations == 1
    assert len(apply_recorder) == 1  # accepted + auto_apply → patched
    assert apply_recorder[0][1] == "- old\n+ new"


async def test_tune_does_not_escalate_when_healthy(tmp_path):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"  # empty store → no stall
    imp_factory, _ = _improve_factory([_report(hqs_before=0.70, needs_improvement=True)])
    opt_factory, opt = _optimize_factory(_opt_result())
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, harness_factory=_harness_factory(db),
        improve_factory=imp_factory, optimize_factory=opt_factory,
        max_iterations=3,
    )
    res = await runner.run()
    assert res.stop_reason == "max_iterations"
    assert opt.calls == 0
    assert res.escalations == 0


async def test_tune_respects_max_escalations(tmp_path, apply_recorder):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"
    await _seed_stalled_oscillation(idb)
    # never converges, always stalled → escalate twice then cap
    imp_factory, _ = _improve_factory([_report(hqs_before=0.60, needs_improvement=True)])
    opt_factory, opt = _optimize_factory(_opt_result(accepted=True))
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, max_escalations=2, max_iterations=10, auto_apply=True,
        harness_factory=_harness_factory(db), improve_factory=imp_factory,
        optimize_factory=opt_factory,
    )
    res = await runner.run()
    assert res.stop_reason == "escalation_cap"
    assert opt.calls == 2
    assert res.escalations == 2
    assert len(apply_recorder) == 2


async def test_tune_budget_stop_llm_calls(tmp_path):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"  # empty → no stall, no escalate
    imp_factory, _ = _improve_factory([_report(hqs_before=0.60, needs_improvement=True)])
    opt_factory, opt = _optimize_factory(_opt_result())
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, max_llm_calls=5, max_iterations=100,
        harness_factory=_harness_factory(db, calls_per_iter=2),
        improve_factory=imp_factory, optimize_factory=opt_factory,
    )
    res = await runner.run()
    # iter1 acc=2 (<5); iter2 acc=4 (<5); iter3 acc=6 (>=5) → budget_llm_calls
    assert res.stop_reason == "budget_llm_calls"
    assert res.llm_calls == 6
    assert opt.calls == 0


async def test_tune_no_apply_does_not_patch_spec(tmp_path, apply_recorder):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"
    await _seed_stalled_oscillation(idb)
    imp_factory, _ = _improve_factory([_report(hqs_before=0.60, needs_improvement=True)])
    opt_factory, opt = _optimize_factory(_opt_result(accepted=True))
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, max_escalations=1, max_iterations=10, auto_apply=False,
        harness_factory=_harness_factory(db), improve_factory=imp_factory,
        optimize_factory=opt_factory,
    )
    res = await runner.run()
    # optimize ran (escalation is not gated by auto_apply) but the diff was not applied
    assert opt.calls == 1
    assert res.escalations == 1
    assert apply_recorder == []  # never patched
    # second iteration: still stalled, at cap → escalation_cap
    assert res.stop_reason == "escalation_cap"


async def test_tune_max_iterations(tmp_path):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"  # empty → no stall
    imp_factory, _ = _improve_factory([_report(hqs_before=0.60, needs_improvement=True)])
    opt_factory, opt = _optimize_factory(_opt_result())
    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, max_iterations=3,
        harness_factory=_harness_factory(db), improve_factory=imp_factory,
        optimize_factory=opt_factory,
    )
    res = await runner.run()
    assert res.stop_reason == "max_iterations"
    assert len(res.iterations) == 3
    assert opt.calls == 0


async def test_tune_aborts_on_harness_error(tmp_path):
    db = tmp_path / "traces.db"
    idb = tmp_path / "improvements.db"
    imp_factory, _ = _improve_factory([_report(hqs_before=0.60)])
    opt_factory, opt = _optimize_factory(_opt_result())

    class _Boom:
        _run_id = "boom"
        async def run(self, inputs):
            raise RuntimeError("kaboom")

    runner = TuneRunner(
        spec_path=tmp_path / "wf.yaml", inputs={}, trace_db=db, improvement_db=idb,
        target_hqs=0.90, max_iterations=5,
        harness_factory=lambda spec_path: _Boom(),
        improve_factory=imp_factory, optimize_factory=opt_factory,
    )
    res = await runner.run()
    assert res.stop_reason == "error"
    assert opt.calls == 0
    assert len(res.iterations) == 0