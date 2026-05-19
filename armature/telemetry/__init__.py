"""Optional OpenTelemetry instrumentation for Armature.

If ``opentelemetry-sdk`` is not installed, all functions degrade silently to
no-ops. Callers must never assume OTel is present — always use get_tracer()
and call methods on the returned object.
"""
from __future__ import annotations

try:
    from opentelemetry import trace as _otel_trace
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


def configure(endpoint: str | None = None) -> None:
    """Set up an OTel TracerProvider.

    Call once at process startup before the first Harness.run().

    Args:
        endpoint: OTLP gRPC endpoint (e.g. "http://localhost:4317").
                  When None, spans are exported to an in-process
                  InMemorySpanExporter — useful for testing only.
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

    The returned object always has a start_as_current_span(name, **kw)
    context-manager method and spans always support set_attribute,
    record_exception, and set_status.
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

    def set_attribute(self, key: str, value: object) -> None:
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def set_status(self, *args: object) -> None:
        pass


class _NoOpTracer:
    """Drop-in for an OTel Tracer when the SDK is not installed."""

    def start_as_current_span(self, name: str, **kwargs: object) -> "_NoOpSpan":
        return _NoOpSpan()
