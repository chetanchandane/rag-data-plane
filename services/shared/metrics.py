"""CloudWatch metrics as Embedded Metric Format (EMF) log lines (SPEC: Proof, "SLO targets";
DECISIONS 2026-11-25, "Step 25").

On Lambda, a line printed to stdout reaches CloudWatch Logs as is (both functions log in
Text format), and CloudWatch Logs turns every line shaped like EMF into metric values. So
there is no API call in the request path, no IAM permission and no dependency.

One line is one JSON object. Its `_aws` member names the metrics and their dimensions; the
values sit at the top level beside them. Any other top-level key is a property: not a
metric, free, and queryable in Logs Insights. Each metric name with each set of dimension
values is one billed custom metric, so dimension values come from a short fixed list,
never from ids.

An invocation that crashes or times out prints nothing, so these metrics never see it.
API Gateway's own stage metrics cover that (step 27).
"""

import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

NAMESPACE = "ragdp"
MILLISECONDS = "Milliseconds"
SECONDS = "Seconds"
NO_UNIT = "None"  # a 0/1 value: its Average is a rate, its SampleCount a count

Metrics = Mapping[str, tuple[float, str]]  # name -> (value, unit)


def record(
    metrics: Metrics,
    *,
    dimensions: Mapping[str, str] | None = None,
    properties: Mapping[str, Any] | None = None,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """The EMF object for one set of values that share the same dimensions."""
    dimensions = dimensions or {}
    properties = properties or {}
    if not metrics:
        raise ValueError("an EMF record needs at least one metric")
    # All three share the top level: a clash would silently overwrite a value or dimension.
    clash = (set(properties) & (set(metrics) | set(dimensions))) | (set(metrics) & set(dimensions))
    if clash:
        raise ValueError(f"names used twice in one EMF record: {sorted(clash)}")
    definitions = [{"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()]
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000) if timestamp_ms is None else timestamp_ms,
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    # One dimension set. An empty one is allowed: a metric with no dimensions.
                    "Dimensions": [list(dimensions)],
                    "Metrics": definitions,
                }
            ],
        },
        **properties,
        **dimensions,
        **{name: value for name, (value, _) in metrics.items()},
    }


def on_lambda() -> bool:
    return "AWS_LAMBDA_FUNCTION_NAME" in os.environ  # set by the Lambda runtime


def emit(
    metrics: Metrics,
    *,
    dimensions: Mapping[str, str] | None = None,
    properties: Mapping[str, Any] | None = None,
) -> None:
    """Print the record as one stdout line on Lambda. Elsewhere (api.local, worker.local,
    tests) do nothing: there's no CloudWatch to read it.

    Best effort: a metric that can't be built or printed is logged and dropped. It must never
    turn a 200 into a 500, or an indexed document into an SQS retry."""
    if not on_lambda():
        return
    try:
        rec = record(metrics, dimensions=dimensions, properties=properties)
        print(json.dumps(rec, default=str), flush=True)
    except Exception:
        log.exception("metric dropped: %s", sorted(metrics))
