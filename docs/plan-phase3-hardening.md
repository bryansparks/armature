# Phase 3: Production Hardening — Implementation Plan

## Goal
Harden the Armature runtime for real workloads: typed `on_fail` recovery loops so transient stage failures retry automatically; LLM retry with exponential backoff on rate-limit and service errors; optional OpenTelemetry instrumentation with full no-op graceful degradation; and a Docker deployment package for the HTTP service.

## Architecture

### How the pieces fit together
```
HarnessSpec.stages[n].on_fail: OnFailConfig   ← typed model replaces dict
  └─ OnFailConfig.loop: LoopConfig             ← existing model, now surfaced

Harness._execute_stage_with_recovery()         ← new method, wraps _execute_stage
  └─ retries up to loop.max times              ← enriches context with _retry_attempt

LLMNode._execute_with_escalation()
  └─ calls _call_with_retry()                  ← new helper, replaces direct litellm_completion

armature/telemetry.py                          ← new module
  ├─ get_tracer() → OTel tracer | _NoOpTracer  ← zero-import if otel not installed
  ├─ Harness.run() → armature.run.{name} span
  ├─ Harness._execute_stage() → armature.stage.{id} span
  └─ LLMNode._execute_with_escalation() → armature.llm.completion span

Dockerfile + docker-compose.yml               ← wrap `armature serve` for deployment
```

### Key invariants
- `on_fail` recovery never swallows the final exception — it re-raises after exhausting retries.
- LLM retry only fires on *transient* errors (`RateLimitError`, `ServiceUnavailableError`, `APIConnectionError`). Non-transient errors (`AuthenticationError`, `BadRequestError`) propagate immediately.
- OTel spans are emitted inside try/except — telemetry must never break execution.
- `armature/telemetry.py` has zero top-level imports from `opentelemetry`; all OTel imports are guarded by `try/except ImportError`.

## Tech Stack
- Python 3.11+, existing deps unchanged for Tasks 1–3
- Task 2: `asyncio` (stdlib) + `random` (stdlib) — no new dependency
- Task 3: new optional dep group `[telemetry]` with `opentelemetry-sdk>=1.24` and `opentelemetry-exporter-otlp-proto-grpc>=1.24`
- Task 4: Docker (multi-stage `python:3.11-slim`), `uv` installer

---

## File Map

```
armature/
  spec/
    models.py              ← MODIFY: add OnFailConfig; retype Stage.on_fail
  runtime/
    engine.py              ← MODIFY: add _execute_stage_with_recovery(); wire into run()
  nodes/
    llm.py                 ← MODIFY: add _call_with_retry(); use it in _execute_with_escalation()
  telemetry.py             ← CREATE: get_tracer(), _NoOpTracer, configure()

tests/
  runtime/
    test_recovery.py       ← CREATE: on_fail loop tests
  nodes/
    test_llm.py            ← MODIFY: append retry tests
  test_telemetry.py        ← CREATE: no-op path + OTel-available path

pyproject.toml             ← MODIFY: add [telemetry] optional dep group; add otel to [dev]

Dockerfile                 ← CREATE
docker-compose.yml         ← CREATE
.env.example               ← CREATE
```

---

## Task 1 — Typed `on_fail` + Runtime Recovery Loop

### 1-A  Write the failing tests first

Create `tests/runtime/test_recovery.py`:

```python
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
```

Run to confirm failure:
```
pytest tests/runtime/test_recovery.py -x
```
Expected: `ImportError: cannot import name 'OnFailConfig'`  — tests cannot even import yet.

### 1-B  Add `OnFailConfig` to models.py

In `armature/spec/models.py`:

1. After the existing `LoopConfig` class (line 64), add:

```python
class OnFailConfig(BaseModel):
    loop: LoopConfig | None = None
```

2. Change `Stage.on_fail` from:
```python
    on_fail: dict[str, Any] | None = None
```
to:
```python
    on_fail: OnFailConfig | None = None
```

3. Remove `Any` from the `from typing import Any` import only if it is no longer used anywhere else in the file.  (It is still used by `Adapter.args` and `Signature` — leave the import.)

Resulting relevant section of `models.py` after the edit:

```python
class LoopConfig(BaseModel):
    stage: str
    context: str = "retry"
    max: int = 3
    until: str | None = None


class OnFailConfig(BaseModel):
    loop: LoopConfig | None = None


class Adapter(BaseModel):
    ...

class Stage(BaseModel):
    id: str
    role: Role | None = None
    depends_on: list[str] = Field(default_factory=list)
    adapter: str | None = None
    gate: str | None = None
    signature: Signature | None = None
    output_mode: OutputMode = OutputMode.TEXT
    on_fail: OnFailConfig | None = None
    present: str | None = None
    condition: str | None = None
    output_schema: dict[str, Any] | None = None
    subagent_spec: str | None = None
```

### 1-C  Add `_execute_stage_with_recovery` to `engine.py`

Add this method to the `Harness` class, immediately after `_execute_stage` (around line 111, before `run`):

```python
async def _execute_stage_with_recovery(
    self, stage: Stage, context: dict[str, Any]
) -> Any:
    """Wrap _execute_stage with the on_fail.loop retry policy."""
    if stage.on_fail is None or stage.on_fail.loop is None:
        return await self._execute_stage(stage, context)

    loop_cfg = stage.on_fail.loop
    last_exc: Exception | None = None
    retry_ctx = dict(context)

    for attempt in range(loop_cfg.max + 1):  # attempt 0 is the first (non-retry) call
        try:
            return await self._execute_stage(stage, retry_ctx)
        except Exception as exc:
            last_exc = exc
            if attempt < loop_cfg.max:
                retry_ctx = {
                    **retry_ctx,
                    "_retry_attempt": attempt + 1,
                    "_last_error": str(exc),
                }

    raise last_exc  # type: ignore[misc]
```

### 1-D  Wire `_execute_stage_with_recovery` into `run()`

In `engine.py`, inside `run()`, change the `make_handler` inner function from:

```python
        async def make_handler(stage: Stage):
            async def handler(ctx):
                return await self._execute_stage(stage, ctx)
            return handler
```

to:

```python
        async def make_handler(stage: Stage):
            async def handler(ctx):
                return await self._execute_stage_with_recovery(stage, ctx)
            return handler
```

### 1-E  Verify tests pass

```
pytest tests/runtime/test_recovery.py -v
```

All five tests should be green. Then run the full suite to confirm no regressions:

```
pytest --tb=short -q
```

### 1-F  Commit

```
git add armature/spec/models.py armature/runtime/engine.py tests/runtime/test_recovery.py
git commit -m "feat: typed OnFailConfig model + _execute_stage_with_recovery retry loop"
```

---

## Task 2 — LLM Retry with Exponential Backoff

### 2-A  Write the failing tests first

Append to `tests/nodes/test_llm.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: LLM retry with exponential backoff
# ---------------------------------------------------------------------------

async def test_retries_on_rate_limit_and_succeeds():
    """Should retry on RateLimitError and eventually return a valid result."""
    import asyncio

    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise litellm.RateLimitError(
                message="rate limited",
                llm_provider="openai",
                model="test-model",
            )
        return make_litellm_response("hello")

    with patch("armature.nodes.llm.litellm_completion", side_effect=mock_completion), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await node.execute({})

    assert call_count == 3
    assert result.get("content") == "hello"


async def test_raises_after_max_retries():
    """Should re-raise RateLimitError after exhausting all retries."""
    stage = make_stage(RoleType.WORKER)
    tiers = make_tiers()
    node = LLMNode(stage=stage, tiers=tiers)

    async def always_rate_limit(**kwargs):
        raise litellm.RateLimitError(
            message="always limited",
            llm_provider="openai",
            model="test-model",
        )

    with patch("armature.nodes.llm.litellm_completion", side_effect=always_rate_limit), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(litellm.RateLimitError):
            await node.execute({})


async def test_non_transient_error_not_retried():
    """AuthenticationError should propagate immediately without any retry."""
    stage = make_stage(RoleType.WORKER)
    tiers = ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini"))
    node = LLMNode(stage=stage, tiers=tiers)

    call_count = 0

    async def auth_error(**kwargs):
        nonlocal call_count
        call_count += 1
        raise litellm.AuthenticationError(
            message="bad key",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    with patch("armature.nodes.llm.litellm_completion", side_effect=auth_error), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(litellm.AuthenticationError):
            await node.execute({})

    assert call_count == 1  # fired once, not retried
```

Also add `import litellm` near the top of the test file (it's already available as a transitive dep).

Run to confirm failure:
```
pytest tests/nodes/test_llm.py::test_retries_on_rate_limit_and_succeeds -x
```
Expected: test calls `litellm_completion` once then raises — no retry logic exists yet.

### 2-B  Implement `_call_with_retry` in `llm.py`

In `armature/nodes/llm.py`, after the imports and before `_TIER_ORDER`, add:

```python
import asyncio
import random


def _retryable_errors() -> tuple[type[Exception], ...]:
    """Return the set of litellm errors that warrant a retry."""
    errors: list[type[Exception]] = []
    for name in ("RateLimitError", "ServiceUnavailableError", "APIConnectionError", "Timeout"):
        cls = getattr(litellm, name, None)
        if cls is not None:
            errors.append(cls)
    return tuple(errors) if errors else (Exception,)


async def _call_with_retry(
    model: str,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any:
    """Call litellm_completion with exponential backoff on transient errors.

    Non-transient errors (AuthenticationError, BadRequestError, etc.) propagate
    immediately without retrying.
    """
    retryable = _retryable_errors()
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            return await litellm_completion(model=model, **kwargs)
        except retryable as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = (2 ** attempt) + random.uniform(0.0, 0.5)
                await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]
```

### 2-C  Use `_call_with_retry` in `_execute_with_escalation`

In `_execute_with_escalation`, change the single line:

```python
            response = await litellm_completion(model=model, **base_kwargs)
```

to:

```python
            response = await _call_with_retry(model=model, **base_kwargs)
```

No other changes to the method.

### 2-D  Verify

```
pytest tests/nodes/test_llm.py -v
```

All tests should pass including the three new ones. Full suite:

```
pytest --tb=short -q
```

### 2-E  Commit

```
git add armature/nodes/llm.py tests/nodes/test_llm.py
git commit -m "feat: LLM retry with exponential backoff on transient litellm errors"
```

---

## Task 3 — OpenTelemetry Instrumentation

### 3-A  Add OTel to pyproject.toml

In `pyproject.toml`, under `[project.optional-dependencies]`, add after the `service` group:

```toml
telemetry = [
    "opentelemetry-sdk>=1.24",
    "opentelemetry-exporter-otlp-proto-grpc>=1.24",
]
```

Also extend `dev` to install the SDK so tests can exercise the real OTel path:

```toml
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "opentelemetry-sdk>=1.24",
]
```

### 3-B  Write failing tests

Create `tests/test_telemetry.py`:

```python
"""Tests for armature.telemetry — both the no-op path and the real OTel path."""
import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# No-op path — always runnable, no OTel dep needed
# ---------------------------------------------------------------------------

def test_noop_tracer_has_start_as_current_span():
    from armature.telemetry import _NoOpTracer
    tracer = _NoOpTracer()
    span_cm = tracer.start_as_current_span("test-span")
    with span_cm as span:
        span.set_attribute("key", "value")   # must not raise
        span.record_exception(ValueError("x"))
        span.set_status("OK")


def test_get_tracer_returns_noop_when_otel_missing():
    import armature.telemetry as tel
    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = False
        tracer = tel.get_tracer()
        assert isinstance(tracer, tel._NoOpTracer)
    finally:
        tel._OTEL_AVAILABLE = original


def test_configure_is_noop_when_otel_missing():
    import armature.telemetry as tel
    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = False
        tel.configure()   # must not raise
    finally:
        tel._OTEL_AVAILABLE = original


# ---------------------------------------------------------------------------
# Real OTel path — requires opentelemetry-sdk (in [dev])
# ---------------------------------------------------------------------------

otel = pytest.importorskip("opentelemetry", reason="opentelemetry-sdk not installed")


def test_get_tracer_returns_real_tracer_when_otel_available():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry import trace
    import armature.telemetry as tel

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = True
        tracer = tel.get_tracer()
        with tracer.start_as_current_span("unit-test-span") as span:
            span.set_attribute("hello", "world")
    finally:
        tel._OTEL_AVAILABLE = original

    finished = exporter.get_finished_spans()
    names = [s.name for s in finished]
    assert "unit-test-span" in names
    attrs = {s.name: dict(s.attributes) for s in finished}
    assert attrs["unit-test-span"]["hello"] == "world"


async def test_engine_emits_run_span(tmp_path):
    """Harness.run() should emit a span named armature.run.<workflow>."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry import trace
    from unittest.mock import AsyncMock
    import armature.telemetry as tel
    from armature.runtime.engine import Harness
    from armature.spec.models import HarnessSpec, Stage, Role, RoleType

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = True
        spec = HarnessSpec(
            name="otel-workflow",
            version="1.0",
            stages=[Stage(id="s1", role=Role(name="r", type=RoleType.WORKER, description="t"))],
        )
        harness = Harness(spec=spec, session_dir=tmp_path)

        with patch.object(harness, "_execute_stage", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = {"content": "ok"}
            await harness.run({})
    finally:
        tel._OTEL_AVAILABLE = original

    span_names = [s.name for s in exporter.get_finished_spans()]
    assert any("armature.run." in n for n in span_names), f"No run span in: {span_names}"


async def test_engine_emits_stage_span(tmp_path):
    """_execute_stage() should emit a span named armature.stage.<id>."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry import trace
    from unittest.mock import AsyncMock, patch
    import armature.telemetry as tel
    from armature.runtime.engine import Harness
    from armature.spec.models import HarnessSpec, Stage, Role, RoleType

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = True
        spec = HarnessSpec(
            name="stage-span-test",
            version="1.0",
            stages=[Stage(id="my-stage", role=Role(name="r", type=RoleType.WORKER, description="t"))],
        )
        harness = Harness(spec=spec, session_dir=tmp_path)
        stage = spec.stages[0]

        with patch("armature.nodes.llm.litellm_completion", new_callable=AsyncMock) as mock_llm:
            from tests.nodes.test_llm import make_litellm_response
            mock_llm.return_value = make_litellm_response("hi")
            await harness._execute_stage(stage, {})
    finally:
        tel._OTEL_AVAILABLE = original

    span_names = [s.name for s in exporter.get_finished_spans()]
    assert "armature.stage.my-stage" in span_names, f"Got spans: {span_names}"
```

Run to confirm failure:
```
pytest tests/test_telemetry.py -x
```
Expected: `ModuleNotFoundError: No module named 'armature.telemetry'`

### 3-C  Create `armature/telemetry.py`

```python
"""Optional OpenTelemetry instrumentation for Armature.

If ``opentelemetry-sdk`` is not installed, all functions degrade silently to
no-ops. Callers must never assume OTel is present — always use get_tracer()
and call methods on the returned object.
"""
from __future__ import annotations

try:
    from opentelemetry import trace as _otel_trace
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover — guarded branch for production without OTel
    _OTEL_AVAILABLE = False


def configure(endpoint: str | None = None) -> None:
    """Set up an OTel TracerProvider.

    Call once at process startup before the first ``Harness.run()``.

    Args:
        endpoint: OTLP gRPC endpoint (e.g. ``"http://localhost:4317"``).
                  When *None*, spans are exported to an in-process
                  ``InMemorySpanExporter`` — useful for testing only.
    """
    if not _OTEL_AVAILABLE:
        return

    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import]
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # type: ignore[import]

    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import]

        exporter = OTLPSpanExporter(endpoint=endpoint)
        processor = BatchSpanProcessor(exporter)
    else:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # type: ignore[import]
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        processor = SimpleSpanProcessor(exporter)

    provider = TracerProvider()
    provider.add_span_processor(processor)
    _otel_trace.set_tracer_provider(provider)


def get_tracer() -> object:
    """Return a real OTel tracer or a no-op stand-in.

    The returned object always has a ``start_as_current_span(name, **kw)``
    context-manager method and spans always support ``set_attribute``,
    ``record_exception``, and ``set_status``.
    """
    if _OTEL_AVAILABLE:
        return _otel_trace.get_tracer("armature")
    return _NoOpTracer()


class _NoOpSpan:
    """Drop-in for an OTel Span when the SDK is not installed."""

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def set_attribute(self, key: str, value: object) -> None:  # noqa: ARG002
        pass

    def record_exception(self, exc: BaseException) -> None:  # noqa: ARG002
        pass

    def set_status(self, *args: object) -> None:
        pass


class _NoOpTracer:
    """Drop-in for an OTel Tracer when the SDK is not installed."""

    def start_as_current_span(
        self, name: str, **kwargs: object  # noqa: ARG002
    ) -> "_NoOpSpan":
        return _NoOpSpan()
```

### 3-D  Instrument `engine.py`

At the top of `engine.py`, after the existing imports, add:

```python
from armature.telemetry import get_tracer
```

Wrap `run()` with a top-level span. Change the body of `run()`:

```python
    async def run(self, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(inputs or {})
        context["run_id"] = self._run_id

        tracer = get_tracer()
        with tracer.start_as_current_span(
            f"armature.run.{self._spec.name}",
            attributes={"workflow": self._spec.name, "run_id": self._run_id},
        ):
            await self._session.append(SessionEvent(
                type="run_start", data={"run_id": self._run_id, "workflow": self._spec.name}
            ))

            deps = {s.id: s.depends_on for s in self._spec.stages}

            async def make_handler(stage: Stage):
                async def handler(ctx):
                    return await self._execute_stage_with_recovery(stage, ctx)
                return handler

            handlers = {s.id: await make_handler(s) for s in self._spec.stages}
            executor = DAGExecutor(handlers, deps)
            results = await executor.run(context)

            await self._session.append(SessionEvent(type="run_complete", data={"run_id": self._run_id}))
            return results
```

Wrap `_execute_stage()` with a per-stage span. Add span tracking around the node dispatch block. Replace the `t0 = time.monotonic()` section through to the final `return result` with:

```python
        t0 = time.monotonic()
        tracer = get_tracer()

        with tracer.start_as_current_span(
            f"armature.stage.{stage.id}",
            attributes={"stage": stage.id, "workflow": self._spec.name},
        ) as span:
            try:
                if stage.gate == "human":
                    node = HumanGateNode(stage=stage)
                    result = await node.execute(context)
                elif stage.subagent_spec:
                    from armature.nodes.subagent import SubagentNode
                    node = SubagentNode(stage=stage, session_dir=self._session_dir)
                    result = await node.execute(context)
                elif stage.adapter:
                    adapter = self._spec.adapters.get(stage.adapter)
                    if adapter is None:
                        raise ValueError(f"Adapter '{stage.adapter}' not defined in spec")
                    node = ScriptNode(adapter=adapter)
                    result = await node.execute(context)
                elif stage.role:
                    node = LLMNode(
                        stage=stage,
                        tiers=self._spec.model_tiers,
                        assembler=self._assembler,
                        registry=self._registry,
                    )
                    result = await node.execute(context)
                    # Record trace for LLM stages
                    await self._ensure_traces()
                    latency = (time.monotonic() - t0) * 1000
                    span.set_attribute("latency_ms", latency)
                    output_valid = "_parse_error" not in result
                    await self._traces.record(TraceRecord(
                        run_id=self._run_id,
                        workflow_name=self._spec.name,
                        stage_id=stage.id,
                        role_type=stage.role.type.value,
                        model=node._resolve_model(),
                        input_tokens=result.pop("_input_tokens", 0),
                        output_tokens=result.pop("_output_tokens", 0),
                        latency_ms=latency,
                        success=True,
                        output_valid=output_valid,
                        inputs={k: str(v)[:200] for k, v in context.items()},
                        outputs={k: str(v)[:200] for k, v in result.items()},
                    ))
                else:
                    raise ValueError(f"Stage '{stage.id}' has no role, adapter, or gate")

                span.set_attribute("success", True)
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("success", False)
                raise

        await self._hooks.run_post_stage(stage.id, result, context)
        await self._session.append(SessionEvent(
            type="stage_complete", data={"stage": stage.id, "result": str(result)[:500]}
        ))
        return result
```

### 3-E  Add an LLM completion span in `llm.py`

In `_call_with_retry`, wrap the successful response capture with a span. Replace:

```python
        try:
            return await litellm_completion(model=model, **kwargs)
        except retryable as exc:
```

with:

```python
        try:
            response = await litellm_completion(model=model, **kwargs)
            try:
                from armature.telemetry import get_tracer
                usage = getattr(response, "usage", None)
                with get_tracer().start_as_current_span(
                    "armature.llm.completion",
                    attributes={
                        "model": model,
                        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                        "attempt": attempt,
                    },
                ):
                    pass  # span purely carries attributes; actual work already done
            except Exception:
                pass  # telemetry must never break execution
            return response
        except retryable as exc:
```

### 3-F  Verify

```
pytest tests/test_telemetry.py -v
pytest --tb=short -q
```

### 3-G  Commit

```
git add armature/telemetry.py armature/runtime/engine.py armature/nodes/llm.py \
        tests/test_telemetry.py pyproject.toml
git commit -m "feat: optional OpenTelemetry instrumentation with no-op fallback"
```

---

## Task 4 — Docker Deployment

No unit tests for this task. Verification is `docker build` succeeding.

### 4-A  Create `Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

# Install uv for fast dependency installation
RUN pip install --no-cache-dir uv==0.4.*

WORKDIR /app

# Copy dependency manifests first (layer cache)
COPY pyproject.toml .
COPY armature/__init__.py armature/__init__.py

# Install runtime deps + service extras (no dev/test deps)
RUN uv pip install --system ".[service]"

# Copy full source
COPY armature/ armature/

# Expose HTTP port
EXPOSE 8080

# Default: run the HTTP service
CMD ["armature", "serve", "--host", "0.0.0.0", "--port", "8080"]
```

Notes:
- The two-step COPY (manifests first, then source) keeps the pip install layer cached unless `pyproject.toml` changes.
- `uv pip install --system` installs into the system Python inside the container.
- The `armature serve` CLI command is already defined in `armature/cli.py`.

### 4-B  Create `docker-compose.yml`

```yaml
version: "3.9"

services:
  armature:
    build: .
    image: armature:latest
    container_name: armature-service
    ports:
      - "${ARMATURE_PORT:-8080}:8080"
    env_file:
      - .env
    environment:
      # Override in .env — these are the defaults
      ARMATURE_LOG_LEVEL: "${ARMATURE_LOG_LEVEL:-info}"
    volumes:
      # Persist trace SQLite DB and session logs across restarts
      - armature_runs:/root/.armature/runs
      # Mount spec files from the host (read-only)
      - "${ARMATURE_SPECS_DIR:-.}/specs:/specs:ro"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    restart: unless-stopped

volumes:
  armature_runs:
```

### 4-C  Create `.env.example`

```dotenv
# -----------------------------------------------------------------------
# Armature HTTP Service — environment configuration
# Copy this file to .env and fill in your values.
# -----------------------------------------------------------------------

# LLM provider API keys
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Optional: OpenRouter (for multi-provider routing)
OPENROUTER_API_KEY=sk-or-...

# Host port to bind the service (default: 8080)
ARMATURE_PORT=8080

# Log level: debug | info | warning | error
ARMATURE_LOG_LEVEL=info

# Directory on the host where YAML workflow specs are stored.
# Mounted read-only at /specs inside the container.
ARMATURE_SPECS_DIR=./specs

# Optional: OpenTelemetry OTLP endpoint (leave blank to disable)
OTEL_EXPORTER_OTLP_ENDPOINT=

# Optional: Armature run storage base dir (default inside container)
# ARMATURE_RUNS_DIR=/root/.armature/runs
```

### 4-D  Verify

```bash
docker build -t armature:latest .
docker compose up --dry-run
```

Both commands should complete without errors.

### 4-E  Commit

```bash
git add Dockerfile docker-compose.yml .env.example
git commit -m "feat: Docker deployment packaging for armature HTTP service"
```

---

## Final verification

After all four tasks are complete:

```bash
# Full test suite with coverage
pytest --cov=armature --cov-report=term-missing -q

# Confirm coverage is ≥82% (should improve from OTel + recovery tests)
# Confirm 77+ tests pass (new tests added: ~13 new)

# Docker smoke test
docker build -t armature:latest .
docker run --rm armature:latest armature --help
```

Expected outcome: all tests green, coverage at or above 85%, Docker image builds and the CLI help text is printed from the container.
