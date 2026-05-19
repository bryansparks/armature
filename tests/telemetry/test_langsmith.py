"""Tests for LangSmith observability adapter."""
import os
import pytest
from armature.hooks.lifecycle import HookRegistry, HookPhase


# ── Fake client ───────────────────────────────────────────────────────────────

class FakeRun:
    def __init__(self, run_id: str):
        self.id = run_id
        self.ended = False
        self.outputs: dict = {}


class FakeLangSmithClient:
    def __init__(self):
        self.runs: list[FakeRun] = []
        self._run_counter = 0

    def create_run(self, **kwargs) -> FakeRun:
        self._run_counter += 1
        run = FakeRun(run_id=f"run-{self._run_counter}")
        self.runs.append(run)
        return run

    def update_run(self, run_id: str, **kwargs):
        for run in self.runs:
            if run.id == run_id:
                run.ended = True
                run.outputs = kwargs.get("outputs", {})
                break


# ── Phase 2-d: LangSmith adapter ─────────────────────────────────────────────

def test_langsmith_adapter_importable():
    from armature.telemetry.langsmith import LangSmithAdapter
    assert LangSmithAdapter is not None


def test_langsmith_is_configured_false_without_env_var(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    from armature.telemetry.langsmith import LangSmithAdapter
    assert LangSmithAdapter.is_configured() is False


def test_langsmith_is_configured_true_with_env_var(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls__test_key")
    from armature.telemetry.langsmith import LangSmithAdapter
    assert LangSmithAdapter.is_configured() is True


def test_langsmith_attach_registers_pre_and_post_stage_hooks():
    from armature.telemetry.langsmith import LangSmithAdapter
    client = FakeLangSmithClient()
    adapter = LangSmithAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf")
    assert len(hooks._hooks[HookPhase.PRE_STAGE]) >= 1
    assert len(hooks._hooks[HookPhase.POST_STAGE]) >= 1


async def test_langsmith_pre_stage_creates_run():
    from armature.telemetry.langsmith import LangSmithAdapter
    client = FakeLangSmithClient()
    adapter = LangSmithAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf")

    await hooks.run_pre_stage("stage1", {})
    assert len(client.runs) == 1


async def test_langsmith_post_stage_ends_run():
    from armature.telemetry.langsmith import LangSmithAdapter
    client = FakeLangSmithClient()
    adapter = LangSmithAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf")

    await hooks.run_pre_stage("stage1", {})
    await hooks.run_post_stage("stage1", {"answer": "42"}, {})
    assert client.runs[0].ended is True


async def test_langsmith_multiple_stages_create_separate_runs():
    from armature.telemetry.langsmith import LangSmithAdapter
    client = FakeLangSmithClient()
    adapter = LangSmithAdapter(client=client)
    hooks = HookRegistry()
    adapter.attach(hooks, run_id="r1", workflow_name="wf")

    await hooks.run_pre_stage("stage_a", {})
    await hooks.run_post_stage("stage_a", {}, {})
    await hooks.run_pre_stage("stage_b", {})
    await hooks.run_post_stage("stage_b", {}, {})
    assert len(client.runs) == 2
    assert all(r.ended for r in client.runs)
