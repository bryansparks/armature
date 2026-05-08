"""Tests for the Quorum deliberation skill."""
import pytest
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
