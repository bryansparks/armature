"""Tests for armature.telemetry — both the no-op path and the real OTel path."""
import pytest
from unittest.mock import patch, AsyncMock


# ---------------------------------------------------------------------------
# No-op path — always runnable, no OTel dep needed
# ---------------------------------------------------------------------------

def test_noop_tracer_has_start_as_current_span():
    from armature.telemetry import _NoOpTracer
    tracer = _NoOpTracer()
    span_cm = tracer.start_as_current_span("test-span")
    with span_cm as span:
        span.set_attribute("key", "value")
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
        tel.configure()  # must not raise
    finally:
        tel._OTEL_AVAILABLE = original


# ---------------------------------------------------------------------------
# Real OTel path — requires opentelemetry-sdk (in [dev])
# ---------------------------------------------------------------------------

otel = pytest.importorskip("opentelemetry", reason="opentelemetry-sdk not installed")


# OTel 1.x does not allow overriding a TracerProvider once set.
# All real-OTel tests share a single provider + exporter configured at
# module import time; each test calls exporter.clear() before asserting.
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry import trace as _otel_trace

_shared_exporter = InMemorySpanExporter()
_shared_provider = TracerProvider()
_shared_provider.add_span_processor(SimpleSpanProcessor(_shared_exporter))
_otel_trace.set_tracer_provider(_shared_provider)


def test_get_tracer_returns_real_tracer_when_otel_available():
    import armature.telemetry as tel

    _shared_exporter.clear()
    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = True
        tracer = tel.get_tracer()
        with tracer.start_as_current_span("unit-test-span") as span:
            span.set_attribute("hello", "world")
    finally:
        tel._OTEL_AVAILABLE = original

    finished = _shared_exporter.get_finished_spans()
    names = [s.name for s in finished]
    assert "unit-test-span" in names
    attrs = {s.name: dict(s.attributes) for s in finished}
    assert attrs["unit-test-span"]["hello"] == "world"


async def test_engine_emits_run_span(tmp_path):
    """Harness.run() should emit a span named armature.run.<workflow>."""
    import armature.telemetry as tel
    from armature.runtime.engine import Harness
    from armature.spec.models import HarnessSpec, Stage, Role, RoleType

    _shared_exporter.clear()
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

    span_names = [s.name for s in _shared_exporter.get_finished_spans()]
    assert any("armature.run." in n for n in span_names), f"No run span in: {span_names}"


async def test_engine_emits_stage_span(tmp_path):
    """_execute_stage() should emit a span named armature.stage.<id>."""
    import armature.telemetry as tel
    from armature.runtime.engine import Harness
    from armature.spec.models import HarnessSpec, Stage, Role, RoleType, ModelTiers, ModelTierConfig
    from unittest.mock import MagicMock

    _shared_exporter.clear()
    original = tel._OTEL_AVAILABLE
    try:
        tel._OTEL_AVAILABLE = True
        spec = HarnessSpec(
            name="stage-span-test",
            version="1.0",
            stages=[Stage(id="my-stage", role=Role(name="r", type=RoleType.WORKER, description="t"))],
            model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        )
        harness = Harness(spec=spec, session_dir=tmp_path)
        stage = spec.stages[0]

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "hello"
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 5
        mock_response.usage.completion_tokens = 3

        with patch("armature.nodes.llm.litellm_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response
            await harness._execute_stage(stage, {})
    finally:
        tel._OTEL_AVAILABLE = original

    span_names = [s.name for s in _shared_exporter.get_finished_spans()]
    assert "armature.stage.my-stage" in span_names, f"Got spans: {span_names}"
