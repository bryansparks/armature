"""Task 6 Step 2 — smoke-run the ACTUAL campaign nav spec with a mock LLM.

Proves the real artifact
(`experiments/campaign/specs/campaign_research_brief_memory_nav.yml`) is
runnable end-to-end: the Harness constructs, the researcher + judge + curator
stages execute against a mocked litellm, and the run completes without raising.
Also pins the `memory_mode` label so the H4 verdict's nav arm is exercised.

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
    """Load the real nav spec, mock litellm, run one rep, assert it completes."""
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

    async def fake_completion(**kwargs):
        calls["i"] += 1
        n = calls["i"]
        # Dispatch on call index across BOTH patched entry points (ReAct loop +
        # KnowledgeExtractor share this counter — same trick as the e2e test).
        if n == 1:
            # researcher: plain text briefing (captured to L0 memory)
            return _plain_response(
                "Briefing: sub-problem A is X. Sub-problem B is Y. "
                "Sub-problem C is Z."
            )
        if n == 2:
            # judge: guided_json {accept, confidence, issues}
            return _json_content_response(
                {"accept": True, "confidence": 0.8, "issues": []}
            )
        if n == 3:
            # KnowledgeExtractor: one fact (extract_knowledge: true)
            return _extractor_fact_response()
        if n == 4:
            # curator: write a track
            return _tool_call_response(
                "memory.write_track",
                {
                    "track_id": "coverage", "title": "Coverage",
                    "summary": "Covered A, B, C.", "evidence_links": [],
                },
                call_id="tc_track",
            )
        if n == 5:
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

    # The researcher + judge + curator all executed (>= 6 LLM calls: researcher,
    # judge, extractor, curator write_track, curator write_profile, curator final).
    assert calls["i"] >= 6, (
        f"expected >= 6 LLM calls (researcher+judge+extractor+curator), "
        f"got {calls['i']}"
    )