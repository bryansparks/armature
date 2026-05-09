import pytest
from unittest.mock import AsyncMock
from armature.spec.models import (
    HarnessSpec, Stage, Role, RoleType, OnFailConfig, LoopConfig,
)


def _spec_with_on_fail(max_retries: int) -> HarnessSpec:
    return HarnessSpec(
        name="recovery-test",
        version="1.0",
        stages=[
            Stage(
                id="s1",
                role=Role(name="r", type=RoleType.WORKER, description="test"),
                on_fail=OnFailConfig(loop=LoopConfig(stage="s1", max=max_retries)),
            )
        ],
    )


async def test_recovery_retries_and_eventually_succeeds(tmp_path):
    from armature.runtime.engine import Harness

    spec = _spec_with_on_fail(max_retries=2)
    harness = Harness(spec=spec, session_dir=tmp_path)

    call_count = 0

    async def fake_execute_stage(stage, context):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RuntimeError("transient failure")
        return {"output": "ok"}

    harness._execute_stage = fake_execute_stage
    result = await harness._execute_stage_with_recovery(spec.stages[0], {})
    assert call_count == 2
    assert result["output"] == "ok"


async def test_recovery_exhausts_retries_and_reraises(tmp_path):
    from armature.runtime.engine import Harness

    spec = _spec_with_on_fail(max_retries=2)
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._execute_stage = AsyncMock(side_effect=RuntimeError("always fails"))

    with pytest.raises(RuntimeError, match="always fails"):
        await harness._execute_stage_with_recovery(spec.stages[0], {})

    # 1 initial attempt + 2 retries = 3 total
    assert harness._execute_stage.call_count == 3


async def test_no_on_fail_propagates_exception_immediately(tmp_path):
    from armature.runtime.engine import Harness
    from armature.spec.models import HarnessSpec, Stage, Role, RoleType

    spec = HarnessSpec(
        name="no-recovery",
        version="1.0",
        stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="test"))],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._execute_stage = AsyncMock(side_effect=RuntimeError("immediate"))

    with pytest.raises(RuntimeError, match="immediate"):
        await harness._execute_stage_with_recovery(spec.stages[0], {})

    assert harness._execute_stage.call_count == 1  # no retries


async def test_retry_context_carries_error_info(tmp_path):
    from armature.runtime.engine import Harness

    spec = _spec_with_on_fail(max_retries=1)
    harness = Harness(spec=spec, session_dir=tmp_path)

    captured_contexts: list[dict] = []
    call_count = 0

    async def capture(stage, context):
        nonlocal call_count
        call_count += 1
        captured_contexts.append(dict(context))
        if call_count == 1:
            raise RuntimeError("oops")
        return {"ok": True}

    harness._execute_stage = capture
    await harness._execute_stage_with_recovery(spec.stages[0], {"x": 1})

    assert captured_contexts[0] == {"x": 1}                      # first attempt: clean context
    assert captured_contexts[1]["_retry_attempt"] == 1            # retry: enriched
    assert captured_contexts[1]["_last_error"] == "oops"
    assert captured_contexts[1]["x"] == 1                         # original keys preserved


async def test_on_fail_without_loop_propagates_immediately(tmp_path):
    """on_fail present but loop=None should not retry."""
    from armature.runtime.engine import Harness

    spec = HarnessSpec(
        name="no-loop",
        version="1.0",
        stages=[
            Stage(
                id="s1",
                role=Role(name="r", type=RoleType.WORKER, description="test"),
                on_fail=OnFailConfig(loop=None),
            )
        ],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._execute_stage = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await harness._execute_stage_with_recovery(spec.stages[0], {})

    assert harness._execute_stage.call_count == 1


async def test_tool_blocked_not_retried(tmp_path):
    """ToolBlocked is a policy violation — retry won't change the outcome."""
    from armature.runtime.engine import Harness
    from armature.hooks.lifecycle import ToolBlocked

    spec = _spec_with_on_fail(max_retries=3)
    harness = Harness(spec=spec, session_dir=tmp_path)
    harness._execute_stage = AsyncMock(side_effect=ToolBlocked("shell", "rm -rf /", "blocked by policy"))

    with pytest.raises(ToolBlocked):
        await harness._execute_stage_with_recovery(spec.stages[0], {})

    assert harness._execute_stage.call_count == 1  # no retries for ToolBlocked


async def test_last_result_in_context_when_until_not_satisfied(tmp_path):
    """When until condition is not met, _last_result is added to retry context."""
    from armature.runtime.engine import Harness

    spec = HarnessSpec(
        name="until-test",
        stages=[Stage(
            id="s1",
            role=Role(name="r", type=RoleType.WORKER, description="t"),
            on_fail=OnFailConfig(loop=LoopConfig(
                stage="s1", max=2, until="{{ status == 'done' }}"
            )),
        )],
    )
    harness = Harness(spec=spec, session_dir=tmp_path)

    captured_contexts: list[dict] = []
    call_count = 0

    async def handler(stage, context):
        nonlocal call_count
        call_count += 1
        captured_contexts.append(dict(context))
        return {"status": "pending"}  # never satisfies until

    harness._execute_stage = handler
    result = await harness._execute_stage_with_recovery(spec.stages[0], {})

    assert call_count == 3  # initial + 2 retries
    assert "_last_result" in captured_contexts[1]  # after first attempt
    assert captured_contexts[1]["_last_result"]["status"] == "pending"
    assert result["status"] == "pending"  # best result returned
