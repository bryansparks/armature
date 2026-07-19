"""Task 6 Step 2 — smoke-run the ACTUAL campaign nav spec with a mock LLM.

Proves the real artifact
(`experiments/campaign/specs/campaign_research_brief_memory_nav.yml`) is
runnable end-to-end AND that Phase-3 navigation actually happens: the
researcher dispatches `memory.search_records` (its result feeds back through
the ReAct loop), the curator's `write_track` / `write_profile` persist to L2/L3,
and the run completes without raising. Also pins the `memory_mode` label so
the H4 verdict's nav arm is exercised.

The Phase-3 assertions give this test teeth: against a no-Phase-3 build (e.g.
main's commit 037a03d, which lacks `_track_store` / `_profile_store` and the
`memory.*` tool handlers), the assertions fail instead of passing vacuously.
Without them this test was a false pass against main's build — it ran the
stages as plain LLM stages with no real navigation.

The nav spec's `model_tiers` point at OpenRouter (needs real API keys); we patch
both module-local `litellm_completion` wrappers
(`armature.nodes.llm.litellm_completion` for the ReAct loop,
`armature.state.extractor.litellm_completion` for the KnowledgeExtractor) with a
shared `fake_completion` that returns plausible guided_json / plain-text /
tool-call responses — the same pattern as
`tests/integration/test_memory_navigation.py::test_curator_writes_track_and_profile_e2e`.

The spec's `memory.db` is overridden to a tmp path so the smoke never touches the
shared warm-spec DB at `~/.armature/memory/campaign-research-brief-memory.db`.
"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

NAV_SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "specs" / "campaign_research_brief_memory_nav.yml"
)


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


def _extractor_fact_response():
    """KnowledgeExtractor: JSON array of {entity, fact, ...} records."""
    r = MagicMock(); r.choices = [MagicMock()]
    content = json.dumps([{
        "entity": "topic",
        "fact": "a sub-problem was covered",
        "confidence": 0.9,
        "source_stage": "researcher",
        "source_key": "content",
        "type": "fact",
    }])
    r.choices[0].message.content = content
    r.choices[0].message.tool_calls = None
    r.usage.prompt_tokens = 10; r.usage.completion_tokens = 5
    return r


@pytest.mark.asyncio
async def test_nav_spec_runs_end_to_end_with_mock_llm(tmp_path, monkeypatch):
    """Load the real nav spec, mock litellm, run one rep, assert navigation
    actually happens (search_records dispatched + curator persists track/profile)
    — not just that the run completes without raising."""
    from armature.spec.loader import load_spec
    from armature.runtime.engine import Harness
    from campaign_runner import fault

    assert NAV_SPEC_PATH.exists(), f"nav spec missing at {NAV_SPEC_PATH}"
    spec = load_spec(NAV_SPEC_PATH)

    # Pin the memory-mode label from the SPEC text (the campaign's source of
    # truth — fault.memory_mode reads the spec file back). navigation_tools:true
    # => "nav", checked before fresh:false would label it "warm".
    assert fault.memory_mode(NAV_SPEC_PATH) == "nav"

    # Override the shared warm-spec DB path so the smoke never pollutes the real
    # accumulated memory at ~/.armature/memory/campaign-research-brief-memory.db.
    spec.memory.db = str(tmp_path / "nav_smoke.db")

    calls = {"i": 0}
    captured_messages: list[list] = []

    async def fake_completion(**kwargs):
        calls["i"] += 1
        n = calls["i"]
        captured_messages.append(kwargs.get("messages") or [])
        # Dispatch on call index across BOTH patched entry points (ReAct loop +
        # KnowledgeExtractor share this counter — same trick as the e2e test).
        if n == 1:
            # researcher: issue a memory.search_records tool call (exercise the
            # READ navigation path — the thesis under test). The real handler
            # runs against the (empty) tmp DB and returns []; the ReAct loop
            # feeds the tool result back as a tool-role message on call 2.
            return _tool_call_response(
                "memory.search_records",
                {"query": "quantum error correction", "top_k": 5},
                call_id="tc_search",
            )
        if n == 2:
            # researcher: final plain-text briefing after the search result.
            return _plain_response(
                "Briefing: sub-problem A is X. Sub-problem B is Y. "
                "Sub-problem C is Z."
            )
        if n == 3:
            # judge: guided_json {accept, confidence, issues}
            return _json_content_response(
                {"accept": True, "confidence": 0.8, "issues": []}
            )
        if n == 4:
            # KnowledgeExtractor: one fact (extract_knowledge: true)
            return _extractor_fact_response()
        if n == 5:
            # curator: write a track
            return _tool_call_response(
                "memory.write_track",
                {
                    "track_id": "coverage", "title": "Coverage",
                    "summary": "Covered A, B, C.", "evidence_links": [],
                },
                call_id="tc_track",
            )
        if n == 6:
            # curator: write the profile
            return _tool_call_response(
                "memory.write_profile",
                {"content": "Team covers broad topics."},
                call_id="tc_profile",
            )
        # curator final: plain text
        return _plain_response("done")

    monkeypatch.setattr("armature.nodes.llm.litellm_completion", fake_completion)
    monkeypatch.setattr(
        "armature.state.extractor.litellm_completion", fake_completion
    )

    # use_cache=False: the global LLM cache would short-circuit the curator's
    # tool-call round-trip (the curator's signature.input filter excludes
    # run_id, so its cache key is stable across runs).
    h = Harness(spec, session_dir=tmp_path, use_cache=False)
    # Should not raise.
    await h.run({"topic": "quantum error correction"})

    # Completion: researcher(search + final) + judge + extractor + curator
    # (write_track + write_profile + final) = 7 LLM calls.
    assert calls["i"] >= 7, (
        f"expected >= 7 LLM calls (researcher+judge+extractor+curator), "
        f"got {calls['i']}"
    )

    # ── Phase-3 teeth: assert navigation ACTUALLY happened, not just that the
    # run didn't raise. Against a no-Phase-3 build (e.g. main's 037a03d), the
    # Harness has no `_track_store`/`_profile_store` attributes and the
    # memory.* tool handlers are not registered — these assertions fail there
    # instead of passing vacuously.
    # (a) The researcher's memory.search_records was dispatched and its result
    #     fed back through the ReAct loop as a tool-role message on call 2.
    roles_call2 = [m.get("role") for m in captured_messages[1]]
    assert "tool" in roles_call2, (
        "researcher's memory.search_records result not seen as a tool-role "
        f"message on the 2nd LLM call (roles={roles_call2}) — navigation was "
        "not dispatched"
    )
    # (b) The curator's write_track / write_profile persisted to L2/L3.
    assert h._track_store is not None, "Harness has no _track_store (no Phase 3)"
    assert await h._track_store.count(spec.name) >= 1, "curator wrote no track"
    track = await h._track_store.get_track(spec.name, "coverage")
    assert track is not None, "track 'coverage' not found after curator run"
    assert h._profile_store is not None, "Harness has no _profile_store (no Phase 3)"
    assert await h._profile_store.get_profile(spec.name) is not None, \
        "curator wrote no profile"