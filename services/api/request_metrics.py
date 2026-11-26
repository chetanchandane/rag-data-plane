"""Per-request SLO metrics (step 25): one EMF line per request, sent by middleware.

Eight custom metrics in all (DECISIONS 2026-11-25, "Step 25"); the API's seven, by Route:

    Latency   ms              retrieve, query   p50/p95/p99 per SLO route
    Fault     0/1, 1 = 5xx    retrieve, query,  Average = error rate, SampleCount = requests
                              other
    Degraded  0/1             query             how often a fallback ran
    CacheHit  0/1             query             cache hit rate

Latency counts only 2xx responses: a fast 4xx would pull p95 down and hide slow answers,
and failures are counted by Fault. For /v1/query, Latency and Degraded count only answers
not served from the cache: a hit takes milliseconds and is never degraded (only validated,
non-degraded answers are stored), so mixing hits in would hide slow answers from the 8 s
p95 as the hit rate rises. CacheHit counts every 2xx. /v1/retrieve's degraded list is a
property (free, in Logs Insights), not a metric, to keep the count at 8.

The routes put `degraded` and `cached` on request.state, because middleware can't read the
response body.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response

from shared import metrics

# Keyed on the matched route's template, not the raw path, so a prefix or a trailing slash
# can't move an SLO route into "other". Anything else, or no match, is "other".
ROUTES = {"/v1/retrieve": "retrieve", "/v1/query": "query"}


async def record_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    status = 500  # an exception that escapes the app reaches the client as a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        emit_request(request, status, (time.perf_counter() - started) * 1000)


def emit_request(request: Request, status: int, latency_ms: float) -> None:
    template = getattr(request.scope.get("route"), "path", None)
    route = ROUTES.get(template, "other") if template else "other"
    ok = 200 <= status < 300
    degraded = getattr(request.state, "degraded", None)
    cached = getattr(request.state, "cached", None)

    values: dict[str, tuple[float, str]] = {"Fault": (1 if status >= 500 else 0, metrics.NO_UNIT)}
    if route == "retrieve" and ok:
        values["Latency"] = (latency_ms, metrics.MILLISECONDS)
    if route == "query" and ok and cached is not None:
        values["CacheHit"] = (1 if cached else 0, metrics.NO_UNIT)
        if not cached:
            values["Latency"] = (latency_ms, metrics.MILLISECONDS)
            values["Degraded"] = (1 if degraded else 0, metrics.NO_UNIT)

    properties: dict[str, Any] = {
        "method": request.method,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "route_template": template,  # /v1/documents/{doc_id}, never the path: no ids
    }
    if degraded is not None:
        properties["degraded"] = degraded
    if cached is not None:
        properties["cached"] = cached
    metrics.emit(values, dimensions={"Route": route}, properties=properties)
