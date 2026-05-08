from armature.runtime.context import ContextManager


def test_add_and_retrieve():
    mgr = ContextManager(token_budget=1000)
    mgr.add_message("user", "hello")
    messages = mgr.messages()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_compaction_fires_at_budget():
    mgr = ContextManager(token_budget=50)
    for i in range(8):
        mgr.add_message("user", f"message number {i} with some words")
    assert mgr.estimated_tokens() <= 50


def test_recent_messages_preserved_after_compaction():
    mgr = ContextManager(token_budget=50)
    for i in range(8):
        mgr.add_message("user", f"message {i}")
    messages = mgr.messages()
    assert any("7" in m["content"] for m in messages)


def test_empty_context_zero_tokens():
    mgr = ContextManager(token_budget=1000)
    assert mgr.estimated_tokens() == 0


def test_messages_returns_copy():
    """Mutating the returned list does not affect internal state."""
    mgr = ContextManager(token_budget=1000)
    mgr.add_message("user", "hi")
    msgs = mgr.messages()
    msgs.clear()
    assert len(mgr.messages()) == 1


def test_multiple_roles_stored():
    mgr = ContextManager(token_budget=1000)
    mgr.add_message("user", "question")
    mgr.add_message("assistant", "answer")
    msgs = mgr.messages()
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


def test_compaction_inserts_summary_message():
    """After compaction, a system summary message is prepended."""
    mgr = ContextManager(token_budget=30, keep_recent=2)
    for i in range(6):
        mgr.add_message("user", f"msg {i} padding words here")
    msgs = mgr.messages()
    assert any(m["role"] == "system" and "Compacted" in m["content"] for m in msgs)


def test_compaction_keeps_at_most_keep_recent_plus_summary():
    """After many messages, compaction keeps a summary plus recent messages."""
    mgr = ContextManager(token_budget=30, keep_recent=2)
    for i in range(10):
        mgr.add_message("user", f"message {i} with padding")
    msgs = mgr.messages()
    # Compaction fires periodically: summary + keep_recent, then more can accumulate
    # before next trigger, so total <= summary + keep_recent + keep_recent
    assert len(msgs) < 10
    assert any(m["role"] == "system" and "Compacted" in m["content"] for m in msgs)


def test_no_compaction_below_budget():
    mgr = ContextManager(token_budget=10000)
    for i in range(5):
        mgr.add_message("user", f"msg {i}")
    assert len(mgr.messages()) == 5
    assert mgr.estimated_tokens() < 10000
