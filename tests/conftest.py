"""Fixtures shared by every test package."""

import functools
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@functools.cache
def _memory_exporter() -> InMemorySpanExporter:
    """Installed once: OpenTelemetry allows one global tracer provider per process. Simple
    (not batch) processing, so a span is readable as soon as it ends. Nothing leaves the
    process."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """The spans ended during the test (call .get_finished_spans())."""
    exporter = _memory_exporter()
    exporter.clear()
    yield exporter
    exporter.clear()
