from armature.runtime.context import ContextManager

def test_add_and_retrieve():
    mgr = ContextManager(token_budget=1000)
    mgr.add_message("user", "hello")
    messages = mgr.messages()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"

def test_compaction_fires_at_budget():
    mgr = ContextManager(token_budget=50)
    # Each message roughly 10 tokens
    for i in range(8):
        mgr.add_message("user", f"message number {i} with some words")
    # After compaction, context should be smaller
    assert mgr.estimated_tokens() <= 50

def test_recent_messages_preserved_after_compaction():
    mgr = ContextManager(token_budget=50)
    for i in range(8):
        mgr.add_message("user", f"message {i}")
    messages = mgr.messages()
    # Most recent message should always be preserved
    assert any("7" in m["content"] for m in messages)
