"""services/shared/metrics.py: the EMF record's exact shape, and emit() printing only on
Lambda. No AWS."""

import json

import pytest

from shared import metrics
from tests.emf import check, emitted


def test_the_record_has_the_exact_emf_shape():
    rec = metrics.record(
        {"Latency": (12.5, metrics.MILLISECONDS), "Fault": (0, metrics.NO_UNIT)},
        dimensions={"Route": "retrieve"},
        properties={"status": 200},
        timestamp_ms=1_764_000_000_000,
    )
    assert rec == {
        "_aws": {
            "Timestamp": 1_764_000_000_000,
            "CloudWatchMetrics": [
                {
                    "Namespace": "ragdp",
                    "Dimensions": [["Route"]],
                    "Metrics": [
                        {"Name": "Latency", "Unit": "Milliseconds"},
                        {"Name": "Fault", "Unit": "None"},
                    ],
                }
            ],
        },
        "status": 200,
        "Route": "retrieve",
        "Latency": 12.5,
        "Fault": 0,
    }
    check(rec)


def test_no_dimensions_is_one_empty_dimension_set():
    rec = metrics.record({"IngestFreshness": (41.0, metrics.SECONDS)})
    assert rec["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]
    check(rec)


def test_the_timestamp_defaults_to_now_in_milliseconds(monkeypatch):
    monkeypatch.setattr(metrics.time, "time", lambda: 1_764_000_000.25)
    rec = metrics.record({"Fault": (0, metrics.NO_UNIT)})
    assert rec["_aws"]["Timestamp"] == 1_764_000_000_250


@pytest.mark.parametrize(
    ("dimensions", "properties"),
    [
        ({}, {"Fault": "x"}),  # a property would overwrite a value
        ({"Route": "query"}, {"Route": "other"}),  # ... or a dimension
        ({"Fault": "x"}, {}),  # a dimension named like a metric
    ],
)
def test_a_name_used_twice_is_refused(dimensions, properties):
    with pytest.raises(ValueError, match="used twice"):
        metrics.record(
            {"Fault": (0, metrics.NO_UNIT)}, dimensions=dimensions, properties=properties
        )


def test_a_record_needs_a_metric():
    with pytest.raises(ValueError, match="at least one metric"):
        metrics.record({})


def test_emit_prints_nothing_off_lambda(monkeypatch, capsys):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    metrics.emit({"Fault": (1, metrics.NO_UNIT)})
    assert capsys.readouterr().out == ""


def test_emit_prints_one_json_line_on_lambda(monkeypatch, capsys):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-api")
    metrics.emit({"Fault": (1, metrics.NO_UNIT)}, dimensions={"Route": "other"})
    out = capsys.readouterr().out
    assert out.count("\n") == 1  # one log event per record
    assert json.loads(out)["Fault"] == 1


def test_emitted_lines_pass_the_spec_checks(monkeypatch, capsys):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "ragdp-api")
    metrics.emit({"Latency": (3.0, metrics.MILLISECONDS)}, dimensions={"Route": "query"})
    [rec] = emitted(capsys)
    assert rec["Route"] == "query"
