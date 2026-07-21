"""Task 7c — rich memory harness suite + credit-gated live test.

Pins every link in the two-layer memory thesis chain mechanically (mock-LLM,
no credits) and wires a credit-gated live test ([F]) that auto-runs the
empirical signal when OpenRouter credits return.

Armature's cross-run memory is two layers: L0 raw stage captures and L1
reconciled knowledge records. Optional read-only navigation tools let stages
query these layers on demand instead of receiving a passive dump. Topic tracks
and team profiles (L2/L3) were explored and removed because they added
complexity without improving measured HQS in the cold-vs-warm campaign.

Tests:
  A — warm populates then nav navigates the SAME shared L1 DB (thesis chain)
  B — navigation returns more relevant L1 facts than the passive _knowledge dump
  C — navigation measurably reduces the context bytes a stage receives
  D — Reconciler dedups across warm runs and the live L1 count stays at 1
  F — credit-gated live nav-vs-cold coverage signal (skipif no API key)

Mock-LLM helpers + the dual-patch trick are copied from `test_nav_spec_smoke.py`
(kept local rather than imported because `tests/` is not a package on sys.path).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import aiosqlite
import pytest


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


async def _seed_knowledge_db(knowledge_store, workflow_name: str, facts: list[tuple[str, str, float]]):
    """Insert live L1 records directly (used to set up measurement scenarios)."""
    from armature.state.knowledge import KnowledgeRecord, MemoryType
    for entity, fact, conf in facts:
        await knowledge_store.record(KnowledgeRecord(
            workflow_name=workflow_name,
            entity=entity,
            fact=fact,
            confidence=conf,
            source_run_id="seed",
            type=MemoryType.FACT,
        ))


def _user_message_bytes(messages: list) -> int:
    """Sum JSON bytes of user-role context messages in one LLM call."""
    total = 0
    for m in messages or []:
        if m.get("role") == "user":
            total += len(json.dumps(m.get("content", "")))
    return total


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

    knowledge_path = hw._knowledge_store._path
    async with aiosqlite.connect(str(knowledge_path)) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE superseded_by IS NULL")
        (n_live,) = await cur.fetchone()
    assert n_live >= 1, "warm run did not populate the shared knowledge DB"

    # ── Phase 2: nav run against the SAME shared DB + namespace, researcher searches ──
    nav_spec = load_spec(NAV_SPEC)
    nav_spec.memory.db = str(shared_db)
    nav_spec.memory.workflow_name = warm_spec.name

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
            return _tool_call_response("memory.search_records", {"query": "sub-problem"})
        if n == 2:
            # The nav researcher saw warm's fact via the tool result and now
            # produces an ADDITIVE briefing: it covers a NEW sub-problem
            # (distributed consensus) instead of repeating A/B.
            return _plain_response(
                "Briefing informed by memory: distributed consensus requires "
                "quorum agreement among replicas."
            )
        if n == 3:
            return _json_content_response({"accept": True, "confidence": 0.85, "issues": []})
        # extractor: a fact semantically distinct from warm's generic fact so the
        # Reconciler stores it rather than merging it away.
        return _extractor_fact_response(
            entity="topic",
            fact="distributed consensus requires quorum agreement among replicas",
        )

    _patch_both(monkeypatch, nav_fake)

    hn = Harness(nav_spec, session_dir=tmp_path / "nav", use_cache=False)
    await hn.run({"topic": "the core design problems of distributed systems"})

    assert len(captured_messages) >= 2, "expected a search_records tool-call round-trip"
    roles2 = [m["role"] for m in captured_messages[1]]
    assert "tool" in roles2, "memory.search_records was not dispatched by the ReAct loop"
    tool_contents = " ".join(
        str(m.get("content", "")) for m in captured_messages[1] if m.get("role") == "tool"
    )
    assert "a sub-problem was covered" in tool_contents, (
        "nav search_records did not return warm's extracted fact — "
        "memory.workflow_name alias is not being honored"
    )
    assert captured_user_ctx, "no user context captured"
    researcher_ctx = captured_user_ctx[0]
    assert "_memory_index" in researcher_ctx, \
        "researcher context missing _memory_index (navigation ToC not injected)"
    assert "_knowledge" not in researcher_ctx, \
        "nav researcher received the passive _knowledge dump — suppression failed"
    assert "_memory" not in researcher_ctx, (
        "nav researcher received the passive _memory L0 dump — "
        "signature.input filter not excluding it (thesis-undermining leak)"
    )

    # Validate additive coverage: the nav output extends memory with a new
    # sub-problem, it does not merely echo what was already in L1.
    nav_knowledge_path = hn._knowledge_store._path
    async with aiosqlite.connect(str(nav_knowledge_path)) as db:
        cur = await db.execute(
            "SELECT fact FROM knowledge WHERE workflow_name=? AND superseded_by IS NULL",
            (warm_spec.name,),
        )
        facts = {row[0] for row in await cur.fetchall()}
    assert "distributed consensus requires quorum agreement among replicas" in facts, (
        f"nav run did not add a new distinct L1 fact — facts={facts!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Test B — Navigation relevance beats passive dump (L1 precision)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_navigation_returns_only_relevant_l1_facts(tmp_path, monkeypatch):
    """L1 navigation is precise: the agent's query returns matching records, while
    the passive _knowledge dump uses a fixed query (the workflow name) and
    injects every fact that matches it regardless of the stage's actual topic.

    Setup: 10 facts in the shared DB, all containing the workflow name so the
    passive dump finds them; only 3 also mention the topic query.
    Nav stage's memory.search_records returns only the 3 relevant ones.
    """
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness

    shared_db = tmp_path / "shared.db"
    wf = "campaign-research-brief-memory"
    base = wf  # passive dump searches by workflow name
    facts = [
        # relevant to "distributed systems"
        ("distributed", f"{base}: distributed systems must handle partial failures", 0.9),
        ("consensus", f"{base}: consensus protocols are central to distributed systems", 0.85),
        ("latency", f"{base}: network latency is a fundamental distributed systems constraint", 0.8),
        # irrelevant noise (match passive query but not nav query)
        ("gardening", f"{base}: tomatoes need full sun", 0.9),
        ("cooking", f"{base}: pasta water should be salted", 0.9),
        ("sports", f"{base}: soccer uses a round ball", 0.9),
        ("music", f"{base}: pianos have 88 keys", 0.9),
        ("history", f"{base}: the roman empire fell in 476 ce", 0.9),
        ("astronomy", f"{base}: jupiter is a gas giant", 0.9),
        ("art", f"{base}: oil paint dries slowly", 0.9),
    ]

    # Seed DB before either harness runs.
    seed_spec = load_spec(WARM_SPEC)
    seed_spec.memory.db = str(shared_db)
    seed_spec.memory.extract_knowledge = True
    seed_h = Harness(seed_spec, session_dir=tmp_path / "seed", use_cache=False)
    await seed_h._knowledge_store.init()
    await _seed_knowledge_db(seed_h._knowledge_store, wf, facts)

    captured_passive_ctx: list[dict] = []
    captured_nav_tool_results: list[str] = []

    # Passive fake: just needs to complete the run while we capture the first
    # user message (where _knowledge is injected).
    async def passive_fake(**kwargs):
        msgs = kwargs.get("messages") or []
        for m in msgs:
            if m.get("role") == "user":
                try:
                    captured_passive_ctx.append(json.loads(m["content"]))
                except Exception:
                    pass
        return _plain_response("Briefing: A B C")

    _patch_both(monkeypatch, passive_fake)

    # Passive run (extract_knowledge=True so _knowledge is injected).
    passive_spec = load_spec(WARM_SPEC)
    passive_spec.memory.db = str(shared_db)
    passive_spec.memory.extract_knowledge = True
    hp = Harness(passive_spec, session_dir=tmp_path / "passive", use_cache=False)
    await hp.run({"topic": "distributed systems"})

    # Nav fake: issue a targeted L1 search, capture the tool result, complete.
    nav_calls = {"i": 0}

    async def nav_fake(**kwargs):
        nav_calls["i"] += 1
        n = nav_calls["i"]
        msgs = kwargs.get("messages") or []
        if n == 1:
            return _tool_call_response("memory.search_records", {"query": "distributed systems"})
        if n == 2:
            for m in msgs:
                if m.get("role") == "tool":
                    captured_nav_tool_results.append(str(m.get("content", "")))
            return _plain_response("Briefing informed by memory: D E F")
        if n == 3:
            return _json_content_response({"accept": True, "confidence": 0.8, "issues": []})
        # extractor
        return _extractor_fact_response(entity="topic", fact="new fact")

    _patch_both(monkeypatch, nav_fake)

    # Nav run.
    nav_spec = load_spec(NAV_SPEC)
    nav_spec.memory.db = str(shared_db)
    nav_spec.memory.workflow_name = wf
    hn = Harness(nav_spec, session_dir=tmp_path / "nav", use_cache=False)
    await hn.run({"topic": "distributed systems"})

    # Passive: all 10 facts were dumped into context.
    assert captured_passive_ctx, "no passive user context captured"
    passive_knowledge = captured_passive_ctx[0].get("_knowledge", [])
    passive_facts = {item.get("fact") for item in passive_knowledge}
    assert len(passive_facts) == 10, (
        f"passive dump should inject all 10 facts, got {len(passive_facts)}"
    )
    assert any("tomatoes need full sun" in f for f in passive_facts), (
        "irrelevant fact missing from passive context"
    )

    # Nav: tool result only contains the 3 relevant facts.
    assert captured_nav_tool_results, "no nav tool result captured"
    tool_text = " ".join(captured_nav_tool_results).lower()
    assert "partial failures" in tool_text, "relevant distributed fact missing from nav search"
    assert "consensus protocols" in tool_text, "relevant distributed fact missing from nav search"
    assert "network latency" in tool_text, "relevant distributed fact missing from nav search"
    assert "tomatoes" not in tool_text, "irrelevant fact returned by nav search"
    assert "pasta" not in tool_text, "irrelevant fact returned by nav search"


# ──────────────────────────────────────────────────────────────────────
# Test C — Navigation reduces context bytes vs passive dump
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_navigation_reduces_context_bytes(tmp_path, monkeypatch):
    """Navigation's whole thesis is trading tool-call latency for smaller context.
    This test measures that trade directly: a nav-enabled stage receives far
    fewer bytes of injected context than a passive-injection stage with the
    same L1 records."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness

    shared_db = tmp_path / "shared.db"
    wf = "campaign-research-brief-memory"
    # Include the workflow name in every fact so the passive dump (which
    # queries by workflow name) actually receives the full L1 set.
    facts = [(f"entity{i}", f"{wf}: fact number {i} about the broad research topic", 0.9)
             for i in range(10)]

    seed_spec = load_spec(WARM_SPEC)
    seed_spec.memory.db = str(shared_db)
    seed_spec.memory.extract_knowledge = True
    seed_h = Harness(seed_spec, session_dir=tmp_path / "seed", use_cache=False)
    await seed_h._knowledge_store.init()
    await _seed_knowledge_db(seed_h._knowledge_store, wf, facts)

    passive_bytes = [0]
    nav_bytes = [0]

    async def passive_fake(**kwargs):
        passive_bytes[0] += _user_message_bytes(kwargs.get("messages"))
        return _plain_response("Briefing")

    _patch_both(monkeypatch, passive_fake)

    passive_spec = load_spec(WARM_SPEC)
    passive_spec.memory.db = str(shared_db)
    passive_spec.memory.extract_knowledge = True
    hp = Harness(passive_spec, session_dir=tmp_path / "passive", use_cache=False)
    await hp.run({"topic": "broad research topic"})

    nav_calls = {"i": 0}

    async def nav_fake(**kwargs):
        nav_calls["i"] += 1
        n = nav_calls["i"]
        msgs = kwargs.get("messages") or []
        if n == 1:
            nav_bytes[0] += _user_message_bytes(msgs)
            return _tool_call_response("memory.search_records", {"query": "topic"})
        # Tool result bytes are not counted as "injected context" — they are a
        # response to an explicit agent action. Only count the first nav call.
        if n == 2:
            return _plain_response("Briefing")
        if n == 3:
            return _json_content_response({"accept": True, "confidence": 0.8, "issues": []})
        return _extractor_fact_response(entity="topic", fact="new fact")

    _patch_both(monkeypatch, nav_fake)

    nav_spec = load_spec(NAV_SPEC)
    nav_spec.memory.db = str(shared_db)
    nav_spec.memory.workflow_name = wf
    hn = Harness(nav_spec, session_dir=tmp_path / "nav", use_cache=False)
    await hn.run({"topic": "broad research topic"})

    assert passive_bytes[0] > 0 and nav_bytes[0] > 0, (
        f"must capture context bytes: passive={passive_bytes[0]} nav={nav_bytes[0]}"
    )
    ratio = nav_bytes[0] / passive_bytes[0]
    assert ratio < 0.5, (
        f"nav context ({nav_bytes[0]} bytes) not smaller than passive context "
        f"({passive_bytes[0]} bytes); ratio={ratio:.2%}"
    )


# ──────────────────────────────────────────────────────────────────────
# Test D — Reconcile across runs (dedup keeps L1 clean)
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_dedups_across_warm_runs(tmp_path, monkeypatch):
    """N warm runs whose extractor returns the SAME fact -> the Reconciler
    dedups: the live L1 row count stays at 1, not N. Navigation searches over a
    clean L1, not a growing pile of duplicates. Also quantifies the dedup rate."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness

    shared_db = tmp_path / "shared.db"
    n_runs = 5

    run_idx = [0]

    async def run_warm(monkeypatch):
        run_idx[0] += 1
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
            return _extractor_fact_response(entity="topic", fact="the same fact")

        _patch_both(monkeypatch, fake)
        h = Harness(spec, session_dir=tmp_path / f"warm-{run_idx[0]}", use_cache=False)
        await h.run({"topic": "the core design problems of distributed systems"})
        return h

    h1 = await run_warm(monkeypatch)
    for _ in range(n_runs - 1):
        await run_warm(monkeypatch)

    knowledge_path = h1._knowledge_store._path
    async with aiosqlite.connect(str(knowledge_path)) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM knowledge WHERE superseded_by IS NULL")
        (n_live,) = await cur.fetchone()
    assert n_live == 1, (
        f"Reconciler did not dedup the identical fact across {n_runs} warm runs: "
        f"live count = {n_live} (expected 1).")
    dedup_rate = (n_runs - n_live) / n_runs
    assert dedup_rate >= 0.8, (
        f"Reconciler dedup rate {dedup_rate:.0%} too low for identical facts")


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
    NOT a strict verdict — the real campaign (cold_vs_warm.yml) is the
    serious run. Auto-runs when credits return."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness
    from campaign_runner import hqs, trace_io

    shared_db = tmp_path / "live.db"
    topic = "the core design problems of distributed systems"

    cold_spec = load_spec(WARM_SPEC)
    cold_spec.memory.db = str(shared_db)
    cold_spec.memory.fresh = True
    hc = Harness(cold_spec, session_dir=tmp_path / "cold")
    await hc.run({"topic": topic})
    cold_run_id = hc._run_id
    cold_rows = trace_io.read_rows_by_run(hc._traces._path, cold_run_id)
    cold_q = hqs.avg_quorum(cold_rows)

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
