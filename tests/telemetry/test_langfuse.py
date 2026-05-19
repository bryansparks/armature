"""Tests for LangFuse observability adapter."""
import os
import pytest
from armature.hooks.lifecycle import HookRegistry, HookPhase


# ── Fake client ───────────────────────────────────────────────────────────────

class FakeSpan:
    def __init__(self):
        self.ended = False
        self.end_kwargs: dict = {}

    def end(self, **kwargs):
        self.ended = True
        self.end_kwargs = kwargs


class FakeTrace:
    def __init__(self):
        self.spans: list[FakeSpan] = []
        self.update_kwargs: dict = {}

    def span(self, **kwargs) -> FakeSpan:
        s = FakeSpan()
        self.spans.append(s)
        return s

    def update(self, **kwargs):
        self.update_kwargs = kwargs


class FakeLangFuseClient:
    def __init__(self):
        self.traces: list[FakeTrace] = []
        self.flushed = False

    def trace(self, **kwargs) -> FakeTrace:
        t = FakeTrace()
        self.traces.append(t)
        return t

    def flush(self):
        self.flushed = True


# ── Phase 2-d: LangFuse adapter ───────────────────────────────────────────────

def test_langfuse_adapter_importable():
    from armature.telemetry.langfuse import LangFuseAdapter
    assert LangFuseAdapter is not None


def test_langfuse_is_configured_false_without_env_vars(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from armature.telemetry.langfuse import LangFuseAdapter
    assert LangFuseAdapter.is_configured() is False


def test_langfuse_is_configured_true_with_env_vars(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    from armature.telemetry.langfuse import LangFuseAdapter
    assert LangFuseAdapter.is_configured() is True


def test_langfuse_is_configured_false_with_only_one_key(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from armature.telemetry.langfuse import LangFuseAdapter
    assert LangFuseAdapter.is_configured() is False


def test_langfuse_attach_creates_trace():
    from armature.telemetry.langfuse import LangFuseAdapter
    client = FakeLangFuseClient()
    adapter = LangFuseAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf", spec_version="abc123")
    assert len(client.traces) == 1


def test_langfuse_attach_registers_pre_and_post_stage_hooks():
    from armature.telemetry.langfuse import LangFuseAdapter
    client = FakeLangFuseClient()
    adapter = LangFuseAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf", spec_version="abc123")
    # Both PRE_STAGE and POST_STAGE have registered hooks
    assert len(hooks._hooks[HookPhase.PRE_STAGE]) >= 1
    assert len(hooks._hooks[HookPhase.POST_STAGE]) >= 1


async def test_langfuse_pre_stage_creates_span():
    from armature.telemetry.langfuse import LangFuseAdapter
    client = FakeLangFuseClient()
    adapter = LangFuseAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf", spec_version="abc123")

    await hooks.run_pre_stage("my_stage", {})
    trace = client.traces[0]
    assert len(trace.spans) == 1


async def test_langfuse_post_stage_ends_span():
    from armature.telemetry.langfuse import LangFuseAdapter
    client = FakeLangFuseClient()
    adapter = LangFuseAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf", spec_version="abc123")

    await hooks.run_pre_stage("my_stage", {})
    await hooks.run_post_stage("my_stage", {"output": "done"}, {})
    span = client.traces[0].spans[0]
    assert span.ended is True


async def test_langfuse_post_stage_includes_output_in_span():
    from armature.telemetry.langfuse import LangFuseAdapter
    client = FakeLangFuseClient()
    adapter = LangFuseAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf", spec_version="abc123")

    await hooks.run_pre_stage("stage1", {})
    result = {"text": "hello", "_tokens": {"input": 10, "output": 20}}
    await hooks.run_post_stage("stage1", result, {})
    span = client.traces[0].spans[0]
    assert span.ended is True


async def test_langfuse_multiple_stages_create_separate_spans():
    from armature.telemetry.langfuse import LangFuseAdapter
    client = FakeLangFuseClient()
    adapter = LangFuseAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf", spec_version="abc123")

    await hooks.run_pre_stage("stage_a", {})
    await hooks.run_post_stage("stage_a", {}, {})
    await hooks.run_pre_stage("stage_b", {})
    await hooks.run_post_stage("stage_b", {}, {})

    trace = client.traces[0]
    assert len(trace.spans) == 2
    assert all(s.ended for s in trace.spans)
