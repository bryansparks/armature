"""Task 7c — rich memory-pyramid harness suite + credit-gated live test.

Pins every link in the memory-pyramid thesis chain mechanically (mock-LLM,
no credits) and wires a credit-gated live test ([F]) that auto-runs the
empirical signal when OpenRouter credits return.

Tests:
  A — warm populates then nav navigates the SAME shared L1 DB (thesis chain)
  B — curator persists track + profile; a second nav run reads the track
  D — Reconciler dedups across two warm runs (live L1 count stays at 1)
  F — credit-gated live nav-vs-cold coverage signal (skipif no API key)

Mock-LLM helpers + the dual-patch trick are copied from `test_nav_spec_smoke.py`
(kept local rather than imported because `tests/` is not a package on sys.path).
The accessors used here were verified against `armature/runtime/engine.py`:
  - knowledge DB path: `h._knowledge_store._path` (engine.py:222,255-258 derives
    it as `mem_path.with_name(mem_path.stem + "_knowledge.db")`) — preferred
    over string surgery.
  - `Harness.run()` returns a `results` dict (stage_id -> stage_result), NOT a
    RunResult object — so the run id lives on the Harness as `h._run_id`.
  - trace DB path: `h._traces._path` (a `TraceStore` with `_path`; engine.py:188).
    `trace_io.read_rows_by_run(db_path, run_id)` takes that path.
  - `MemoryConfig.fresh` is a settable bool (spec/models.py:200).
  - warm spec stage order: researcher -> judge (NO extract_knowledge by default
    — the spec comment says "No knowledge extraction"). Tests A and D set
    `warm_spec.memory.extract_knowledge = True` so the extractor runs and L1
    is populated; this is a test-only spec mutation, not a production change.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import aiosqlite
import pytest

# ── Mock-LLM response helpers (copied from test_nav_spec_smoke.py) ──


def _plain_response(content: str):
    r = MagicMock(); r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


def _tool_call_response(tool_name: str, args: dict, call_id: str = "tc_1"):
    r = MagicMock(); r.choices = [MagicMock()]
    tc = MagicMock(); tc.id = call_id; tc.function.name = tool_name
    tc.function.arguments = json.dumps(args)
    r.choices[0].message.tool_calls = [tc]
    r.choices[0].message.content = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


def _json_content_response(payload: dict):
    """guided_json response: content is a JSON string the engine parses."""
    r = MagicMock(); r.choices = [MagicMock()]
    r.choices[0].message.content = json.dumps(payload)
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


def _extractor_fact_response(entity: str = "topic", fact: str = "a sub-problem was covered"):
    """KnowledgeExtractor: JSON array of {entity, fact, ...} records."""
    r = MagicMock(); r.choices = [MagicMock()]
    content = json.dumps([{
        "entity": entity,
        "fact": fact,
        "confidence": 0.9,
        "source_stage": "researcher",
        "source_key": "content",
        "type": "fact",
    }])
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


WARM_SPEC = (
    Path(__file__).resolve().parents[1]
    / "specs" / "campaign_research_brief_memory.yml"
)
NAV_SPEC = (
    Path(__file__).resolve().parents[1]
    / "specs" / "campaign_research_brief_memory_nav.yml"
)


def _patch_both(monkeypatch, fake):
    """Patch the ReAct loop + KnowledgeExtractor litellm entry points with a
    shared fake (the dual-patch trick — one call counter across both)."""
    monkeypatch.setattr("armature.nodes.llm.litellm_completion", fake)
    monkeypatch.setattr("armature.state.extractor.litellm_completion", fake)


# ──────────────────────────────────────────────────────────────────────
# Test A — Shared-DB round-trip (the thesis chain)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_warm_populates_then_nav_navigates_shared_db(tmp_path, monkeypatch):
    """The thesis chain: warm run populates the shared L1 knowledge DB; a nav
    run against the SAME db actually navigates it — the researcher issues
    memory.search_records, the harness dispatches it and returns warm's
    records, and the nav researcher's context carries _memory_index (not the
    passive _knowledge dump)."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness
    from campaign_runner import fault

    assert fault.memory_mode(WARM_SPEC) in ("warm", "cold")  # has memory block
    assert fault.memory_mode(NAV_SPEC) == "nav"

    shared_db = tmp_path / "shared.db"

    # ── Phase 1: warm run populates the shared knowledge DB ──
    warm_spec = load_spec(WARM_SPEC)
    warm_spec.memory.db = str(shared_db)
    # The warm spec disables extract_knowledge by default ("No knowledge
    # extraction"). Turn it on so the KnowledgeExtractor runs and L1 is
    # populated — the test-only mutation that makes the thesis chain testable.
    warm_spec.memory.extract_knowledge = True

    warm_calls = {"i": 0}

    async def warm_fake(**kwargs):
        warm_calls["i"] += 1
        n = warm_calls["i"]
        if n == 1:  # researcher plain text (captured to L0)
            return _plain_response("Briefing: sub-problem A is X. B is Y. C is Z.")
        if n == 2:  # judge guided_json
            return _json_content_response({"accept": True, "confidence": 0.8, "issues": []})
        # extractor: one fact
        return _extractor_fact_response()

    _patch_both(monkeypatch, warm_fake)

    hw = Harness(warm_spec, session_dir=tmp_path / "warm", use_cache=False)
    await hw.run({"topic": "the core design problems of distributed systems"})

    # Assert: the shared knowledge DB has >=1 live record with provenance.
    # Use the Harness's own accessor rather than string surgery on the path.
    knowledge_path = hw._knowledge_store._path
    async with aiosqlite.connect(str(knowledge_path)) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE superseded_by IS NULL")
        (n_live,) = await cur.fetchone()
    assert n_live >= 1, "warm run did not populate the shared knowledge DB"

    # ── Phase 2: nav run against the SAME shared DB, researcher searches ──
    nav_spec = load_spec(NAV_SPEC)
    nav_spec.memory.db = str(shared_db)  # SAME db — the thesis foundation

    nav_calls = {"i": 0}
    captured_user_ctx: list[dict] = []
    captured_messages: list[list] = []

    async def nav_fake(**kwargs):
        nav_calls["i"] += 1
        n = nav_calls["i"]
        msgs = kwargs.get("messages") or []
        captured_messages.append(msgs)
        for m in msgs:
            if m.get("role") == "user":
                try:
                    captured_user_ctx.append(json.loads(m["content"]))
                except Exception:
                    pass
        if n == 1:
            # researcher: issue a memory.search_records tool call
            return _tool_call_response("memory.search_records", {"query": "sub-problem"})
        if n == 2:
            # researcher: tool returned results -> final text briefing
            return _plain_response("Briefing informed by memory: A is X, B is Y.")
        if n == 3:
            # judge guided_json
            return _json_content_response({"accept": True, "confidence": 0.85, "issues": []})
        if n == 4:
            # extractor
            return _extractor_fact_response()
        if n == 5:
            return _tool_call_response("memory.write_track",
                {"track_id": "t", "title": "T", "summary": "S.", "evidence_links": []},
                call_id="tc_t")
        if n == 6:
            return _tool_call_response("memory.write_profile",
                {"content": "Team covers broad topics."}, call_id="tc_p")
        return _plain_response("done")

    _patch_both(monkeypatch, nav_fake)

    hn = Harness(nav_spec, session_dir=tmp_path / "nav", use_cache=False)
    await hn.run({"topic": "the core design problems of distributed systems"})

    # Assert: the researcher actually called memory.search_records (a tool-role
    # message appears in the 2nd ReAct call — the tool was dispatched).
    assert len(captured_messages) >= 2, "expected a search_records tool-call round-trip"
    roles2 = [m["role"] for m in captured_messages[1]]
    assert "tool" in roles2, "memory.search_records was not dispatched by the ReAct loop"
    # Assert: nav researcher context has _memory_index, NOT _knowledge, and
    # NOT the passive _memory L0 dump. The researcher is the first stage; its
    # first LLM call (n=1) is the earliest captured user context. _memory_index
    # lives in the shared context so the judge sees it too, but only the
    # researcher (which declares memory.* tools + has a signature.input
    # whitelist) gets _knowledge and _memory suppressed. We assert against the
    # researcher's context specifically — checking ALL user contexts would
    # false-positive on the judge, which legitimately still receives both.
    assert captured_user_ctx, "no user context captured"
    researcher_ctx = captured_user_ctx[0]
    assert "_memory_index" in researcher_ctx, \
        "researcher context missing _memory_index (navigation ToC not injected)"
    assert "_knowledge" not in researcher_ctx, \
        "nav researcher received the passive _knowledge dump — suppression failed"
    # The nav researcher must NOT receive the passive _memory L0 dump either —
    # navigation is active (tools + _memory_index), not warm+navigation. The
    # spec's signature.input whitelist drops _memory via the llm.py signature
    # filter. If this fails, the nav arm is "warm + navigation" instead of
    # "navigation instead of warm" — a thesis-undermining leak (NEEDS_CONTEXT).
    assert "_memory" not in researcher_ctx, (
        "nav researcher received the passive _memory L0 dump — "
        "signature.input filter not excluding it (thesis-undermining leak)"
    )


# ──────────────────────────────────────────────────────────────────────
# Test B — Curator persistence + read_track round-trip
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_curator_persistence_and_read_track_round_trip(tmp_path, monkeypatch):
    """After a nav run the shared DB has a track + profile; a second nav run
    reads the track via memory.read_track and the harness dispatches it."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness

    shared_db = tmp_path / "shared.db"
    nav_spec = load_spec(NAV_SPEC)
    nav_spec.memory.db = str(shared_db)

    # ── Run 1: researcher text -> judge -> extractor -> curator writes ──
    calls = {"i": 0}

    async def fake1(**kwargs):
        calls["i"] += 1
        n = calls["i"]
        if n == 1:
            return _plain_response("Briefing: A is X. B is Y. C is Z.")
        if n == 2:
            return _json_content_response({"accept": True, "confidence": 0.8, "issues": []})
        if n == 3:
            return _extractor_fact_response()
        if n == 4:
            return _tool_call_response("memory.write_track",
                {"track_id": "coverage", "title": "Coverage",
                 "summary": "Covered A, B, C.", "evidence_links": []},
                call_id="tc_track")
        if n == 5:
            return _tool_call_response("memory.write_profile",
                {"content": "Team covers broad topics."}, call_id="tc_profile")
        return _plain_response("done")

    _patch_both(monkeypatch, fake1)

    h1 = Harness(nav_spec, session_dir=tmp_path / "run1", use_cache=False)
    await h1.run({"topic": "the core design problems of distributed systems"})

    # Assert: L2 track + L3 profile persisted to the shared DB.
    assert await h1._track_store.count(nav_spec.name) >= 1, "curator wrote no track"
    track = await h1._track_store.get_track(nav_spec.name, "coverage")
    assert track is not None, "track 'coverage' not found after curator run"
    # evidence_links may be empty (the mock wrote []); assert what was produced.
    evidence_links = track.get("evidence_links") or []
    if evidence_links:
        # If the curator cited record ids, they must resolve to live L1 rows.
        async with aiosqlite.connect(str(h1._knowledge_store._path)) as db:
            placeholders = ",".join("?" for _ in evidence_links)
            cur = await db.execute(
                f"SELECT COUNT(*) FROM knowledge WHERE id IN ({placeholders}) "
                f"AND superseded_by IS NULL",
                [int(e) for e in evidence_links])
            (n_resolved,) = await cur.fetchone()
        assert n_resolved == len(evidence_links), (
            f"track evidence_links do not all resolve to live knowledge rows: "
            f"{evidence_links} -> {n_resolved}/{len(evidence_links)} live")
    assert await h1._profile_store.get_profile(nav_spec.name) is not None, \
        "curator wrote no profile"

    # ── Run 2: a nav run whose researcher issues memory.read_track ──
    captured_messages: list[list] = []

    async def fake2(**kwargs):
        captured_messages.append(kwargs.get("messages") or [])
        if len(captured_messages) == 1:
            return _tool_call_response("memory.read_track", {"list": True})
        return _plain_response("ok")

    _patch_both(monkeypatch, fake2)

    # Re-create the spec so model_dump state is clean (the Harness computes a
    # spec_version hash at construction; reusing the same mutated object would
    # carry run1's hash into run2's cache key namespace).
    nav_spec2 = load_spec(NAV_SPEC)
    nav_spec2.memory.db = str(shared_db)

    h2 = Harness(nav_spec2, session_dir=tmp_path / "run2", use_cache=False)
    await h2.run({"topic": "the core design problems of distributed systems"})

    # The read tool was actually dispatched: a tool-role message appears in the
    # 2nd ReAct call (mirrors test_second_run_reads_track_via_read_track).
    assert len(captured_messages) >= 2, "expected a read_track tool-call round-trip"
    roles2 = [m["role"] for m in captured_messages[1]]
    assert "tool" in roles2, "memory.read_track was not dispatched by the ReAct loop"


# ──────────────────────────────────────────────────────────────────────
# Test D — Reconcile across runs (dedup keeps L1 clean)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_dedups_across_two_warm_runs(tmp_path, monkeypatch):
    """Two warm runs whose extractor returns the SAME fact both times -> the
    Reconciler dedups: the live L1 row count stays at 1, not 2. Navigation
    searches over a clean L1, not a growing pile of duplicates."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness

    shared_db = tmp_path / "shared.db"

    async def run_warm(monkeypatch):
        spec = load_spec(WARM_SPEC)
        spec.memory.db = str(shared_db)
        spec.memory.extract_knowledge = True  # populate L1 (test-only mutation)

        local = {"i": 0}

        async def fake(**kwargs):
            local["i"] += 1
            n = local["i"]
            if n == 1:
                return _plain_response("Briefing: A is X. B is Y.")
            if n == 2:
                return _json_content_response({"accept": True, "confidence": 0.8, "issues": []})
            # extractor: IDENTICAL fact both runs -> Reconciler SKIP/UPDATE
            return _extractor_fact_response(entity="topic", fact="the same fact")

        _patch_both(monkeypatch, fake)
        h = Harness(spec, session_dir=tmp_path / f"warm-{local}", use_cache=False)
        await h.run({"topic": "the core design problems of distributed systems"})
        return h

    h1 = await run_warm(monkeypatch)
    h2 = await run_warm(monkeypatch)

    # The knowledge DB path is shared; read it via either Harness's accessor.
    knowledge_path = h1._knowledge_store._path
    async with aiosqlite.connect(str(knowledge_path)) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE superseded_by IS NULL")
        (n_live,) = await cur.fetchone()
    assert n_live == 1, (
        f"Reconciler did not dedup the identical fact across two warm runs: "
        f"live count = {n_live} (expected 1). If this is 2, the Reconciler "
        f"regressed — report NEEDS_CONTEXT; do not patch production.")


# ──────────────────────────────────────────────────────────────────────
# Test F — Credit-gated live test (empirical thesis signal)
# ──────────────────────────────────────────────────────────────────────

LIVE = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="needs OPENROUTER_API_KEY + funded OpenRouter credits (deferred)",
)


@LIVE
@pytest.mark.asyncio
async def test_live_nav_coverage_beats_cold(tmp_path):
    """Empirical thesis signal: one real nav rep vs one real cold rep. nav's
    judge coverage (avg quorum) should be >= cold's. Directional 1-rep check,
    NOT a strict verdict — the real campaign (25 reps, cold_vs_warm.yml) is
    the serious run. Auto-runs when credits return."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness
    from campaign_runner import hqs, trace_io

    shared_db = tmp_path / "live.db"
    topic = "the core design problems of distributed systems"

    # Cold rep: warm spec with memory.fresh=True (ignore prior memory, still
    # captures). MemoryConfig.fresh is a settable bool (spec/models.py:200).
    cold_spec = load_spec(WARM_SPEC)
    cold_spec.memory.db = str(shared_db)
    cold_spec.memory.fresh = True
    hc = Harness(cold_spec, session_dir=tmp_path / "cold")
    await hc.run({"topic": topic})
    # Harness.run() returns a results dict (stage_id -> result), NOT a
    # RunResult — the run id lives on the Harness instance (engine.py:143).
    cold_run_id = hc._run_id
    cold_rows = trace_io.read_rows_by_run(hc._traces._path, cold_run_id)
    cold_q = hqs.avg_quorum(cold_rows)

    # Nav rep: nav spec against the SAME shared DB (now populated by cold's
    # captures). The nav spec's db override points at the shared path.
    nav_spec = load_spec(NAV_SPEC)
    nav_spec.memory.db = str(shared_db)
    hn = Harness(nav_spec, session_dir=tmp_path / "nav")
    await hn.run({"topic": topic})
    nav_rows = trace_io.read_rows_by_run(hn._traces._path, hn._run_id)
    nav_q = hqs.avg_quorum(nav_rows)

    assert cold_q is not None and nav_q is not None, \
        f"missing quorum: cold={cold_q} nav={nav_q} (check trace rows)"
    assert nav_q >= cold_q, \
        f"nav coverage {nav_q} < cold coverage {cold_q} — thesis NOT supported on this 1-rep check"