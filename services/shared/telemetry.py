"""Tracing (SPEC: Architecture, Telemetry): OpenTelemetry spans sent to Honeycomb over OTLP/HTTP.

Code creates spans with `trace.get_tracer(__name__)` at import time. Until setup_tracing()
installs a provider, the OpenTelemetry API hands out no-op spans, so the same code runs in
unit tests and in a shell with no key, and nothing is sent.

Tracing must never break ingestion. A failure while setting up is logged and tracing stays
off; the SDK logs and drops spans it can't export.

Span attributes are ids, counts and outcomes, never document text.
"""

import logging
import os
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

log = logging.getLogger(__name__)

# The account is in Honeycomb's US region (the EU one is https://api.eu1.honeycomb.io).
DEFAULT_ENDPOINT = "https://api.honeycomb.io"
EXPORT_TIMEOUT_SECONDS = 5
FLUSH_TIMEOUT_MILLIS = 3000  # what a Lambda invocation may spend sending its spans


def honeycomb_api_key(environ: Mapping[str, str], ssm: Any = None) -> str | None:
    """The ingest key from HONEYCOMB_API_KEY (local .env) or from the SSM SecureString named
    by HONEYCOMB_API_KEY_PARAM (Lambda). None when neither is set: tracing stays off."""
    key = environ.get("HONEYCOMB_API_KEY")
    param = environ.get("HONEYCOMB_API_KEY_PARAM")
    if key and param:
        raise ValueError("set HONEYCOMB_API_KEY or HONEYCOMB_API_KEY_PARAM, not both")
    if param:
        return ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    return key or None


def honeycomb_exporter(api_key: str, endpoint: str = DEFAULT_ENDPOINT) -> SpanExporter:
    # Honeycomb names the dataset after the service.name resource attribute.
    return OTLPSpanExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/traces",
        headers={"x-honeycomb-team": api_key},
        timeout=EXPORT_TIMEOUT_SECONDS,
    )


def build_provider(service_name: str, exporter: SpanExporter) -> TracerProvider:
    """A provider that batches spans in the background and sends them with `exporter`."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def setup_tracing(
    service_name: str, environ: Mapping[str, str] = os.environ, ssm: Any = None
) -> TracerProvider | None:
    """Install the global tracer provider once per process (a Lambda cold start, or the
    local runner). Returns it so the caller can flush it, or None if tracing is off."""
    try:
        api_key = honeycomb_api_key(environ, ssm)
        if api_key is None:
            log.info("tracing off: no Honeycomb key configured")
            return None
        endpoint = environ.get("HONEYCOMB_ENDPOINT", DEFAULT_ENDPOINT)
        provider = build_provider(service_name, honeycomb_exporter(api_key, endpoint))
    except Exception:
        log.exception("tracing off: setup failed")
        return None
    trace.set_tracer_provider(provider)
    log.info("tracing on: %s to %s", service_name, endpoint)
    return provider


def flush(provider: TracerProvider | None) -> None:
    """Send the spans still queued. Lambda freezes the process as soon as the handler
    returns, so spans left in the batch queue would be sent late or never."""
    if provider is None:
        return
    try:
        if not provider.force_flush(FLUSH_TIMEOUT_MILLIS):
            log.warning("tracing: spans not sent within %d ms", FLUSH_TIMEOUT_MILLIS)
    except Exception:
        log.exception("tracing: flush failed")
