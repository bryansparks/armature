"""Tests for the Quorum deliberation skill."""
import pytest
import sys
from unittest.mock import AsyncMock, MagicMock, patch


async def test_quorum_import_error_when_not_installed():
    """Raises ImportError with install instructions when quorum is not installed."""
    from armature.skills.quorum import deliberate
    with patch.dict("sys.modules", {"quorum": None}):
        with pytest.raises(ImportError, match="quorum"):
            await deliberate({"topic": "should I proceed?"})


async def test_quorum_calls_engine_with_topic(monkeypatch):
    """deliberate() passes args.topic as the QuorumConfig objective."""
    captured = {}

    class FakeConfig:
        def __init__(self, objective, documents, agent_roles):
            captured["objective"] = objective
            captured["documents"] = documents
            captured["agent_roles"] = agent_roles

    class FakeResult:
        decision = "proceed"
        confidence = 0.85
        dissenting_opinions = []
        transcript = {}

    class FakeQuorum:
        def __init__(self, config):
            pass
        async def run_async(self):
            return FakeResult()

    fake_module = MagicMock()
    fake_module.Quorum = FakeQuorum
    fake_module.QuorumConfig = FakeConfig

    monkeypatch.setitem(__import__("sys").modules, "quorum", fake_module)
    from importlib import reload
    import armature.skills.quorum as q_mod
    reload(q_mod)

    result = await q_mod.deliberate({
        "topic": "test decision",
        "brief": "some context",
        "agents": ["analyst"],
    })

    assert result["decision"] == "proceed"
    assert result["confidence"] == 0.85
    assert result["dissents"] == []
    assert captured["objective"] == "test decision"
    assert captured["documents"] == ["some context"]
    assert captured["agent_roles"] == ["analyst"]


async def test_quorum_uses_default_agents_when_not_specified(monkeypatch):
    """Uses default agents when not specified."""
    captured = {}

    class FakeConfig:
        def __init__(self, objective, documents, agent_roles):
            captured["agent_roles"] = agent_roles

    class FakeResult:
        decision = "ok"
        confidence = 0.7
        dissenting_opinions = ["risk concern"]
        transcript = {"round": 1}

    class FakeQuorum:
        def __init__(self, config):
            pass
        async def run_async(self):
            return FakeResult()

    fake_module = MagicMock()
    fake_module.Quorum = FakeQuorum
    fake_module.QuorumConfig = FakeConfig

    monkeypatch.setitem(__import__("sys").modules, "quorum", fake_module)
    from importlib import reload
    import armature.skills.quorum as q_mod
    reload(q_mod)

    result = await q_mod.deliberate({"topic": "risk analysis"})

    assert "analyst" in captured["agent_roles"]
    assert result["dissents"] == ["risk concern"]
    assert result["trace"] == {"round": 1}


def _patch_quorum(monkeypatch, *, decision="yes", confidence=0.75, dissents=None, transcript=None):
    """Helper: inject a fake quorum module and reload the skill."""
    captured = {}

    class FakeConfig:
        def __init__(self, objective, documents, agent_roles):
            captured["objective"] = objective
            captured["documents"] = documents
            captured["agent_roles"] = agent_roles

    class FakeResult:
        pass

    fake_result = FakeResult()
    fake_result.decision = decision
    fake_result.confidence = confidence
    fake_result.dissenting_opinions = dissents or []
    fake_result.transcript = transcript or {}

    class FakeQuorum:
        def __init__(self, config): pass
        async def run_async(self): return fake_result

    fake_module = MagicMock()
    fake_module.Quorum = FakeQuorum
    fake_module.QuorumConfig = FakeConfig

    monkeypatch.setitem(sys.modules, "quorum", fake_module)
    from importlib import reload
    import armature.skills.quorum as q_mod
    reload(q_mod)
    return q_mod, captured


async def test_quorum_accepts_objective_as_fallback_for_topic(monkeypatch):
    """deliberate() falls back to args['objective'] if 'topic' is not present."""
    q_mod, captured = _patch_quorum(monkeypatch)
    await q_mod.deliberate({"objective": "fallback objective"})
    assert captured["objective"] == "fallback objective"


async def test_quorum_empty_brief_when_not_specified(monkeypatch):
    """deliberate() uses empty string for brief when omitted."""
    q_mod, captured = _patch_quorum(monkeypatch)
    await q_mod.deliberate({"topic": "something"})
    assert captured["documents"] == [""]


async def test_quorum_returns_all_result_fields(monkeypatch):
    """Return dict always has decision, confidence, dissents, trace."""
    q_mod, _ = _patch_quorum(
        monkeypatch,
        decision="reject",
        confidence=0.3,
        dissents=["too risky", "insufficient data"],
        transcript={"rounds": 3},
    )
    result = await q_mod.deliberate({"topic": "risky move"})
    assert result["decision"] == "reject"
    assert result["confidence"] == 0.3
    assert result["dissents"] == ["too risky", "insufficient data"]
    assert result["trace"] == {"rounds": 3}


async def test_quorum_default_agents_include_risk_assessor(monkeypatch):
    """Default agents include analyst, strategist, and risk_assessor."""
    q_mod, captured = _patch_quorum(monkeypatch)
    await q_mod.deliberate({"topic": "test"})
    assert "analyst" in captured["agent_roles"]
    assert "strategist" in captured["agent_roles"]
    assert "risk_assessor" in captured["agent_roles"]
