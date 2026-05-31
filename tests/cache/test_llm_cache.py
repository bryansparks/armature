"""Unit tests for LLMCache — content-addressed SQLite cache for LLM responses."""


async def test_cache_miss_returns_none(tmp_path):
    from armature.cache.llm_cache import LLMCache
    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    await cache.init()
    result = await cache.get("nonexistent_key")
    assert result is None


async def test_cache_put_and_hit(tmp_path):
    from armature.cache.llm_cache import LLMCache
    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    await cache.init()
    await cache.put("my_key", '{"answer": 42}')
    result = await cache.get("my_key")
    assert result == '{"answer": 42}'


async def test_cache_key_determinism(tmp_path):
    from armature.cache.llm_cache import LLMCache
    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    messages = [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Hello"}]
    extra = {"response_format": {"type": "json_object"}}
    key1 = cache._make_key("gpt-4o", messages, extra)
    key2 = cache._make_key("gpt-4o", messages, extra)
    assert key1 == key2


async def test_cache_key_differs_on_model(tmp_path):
    from armature.cache.llm_cache import LLMCache
    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    messages = [{"role": "user", "content": "Hello"}]
    key1 = cache._make_key("gpt-4o", messages, {})
    key2 = cache._make_key("claude-3-5-sonnet", messages, {})
    assert key1 != key2


async def test_cache_key_differs_on_messages(tmp_path):
    from armature.cache.llm_cache import LLMCache
    cache = LLMCache(tmp_path / "llm_cache.sqlite")
    key1 = cache._make_key("gpt-4o", [{"role": "user", "content": "Hello"}], {})
    key2 = cache._make_key("gpt-4o", [{"role": "user", "content": "Goodbye"}], {})
    assert key1 != key2
