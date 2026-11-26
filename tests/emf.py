"""Test helpers for CloudWatch EMF lines (services/shared/metrics.py).

check() applies the spec rules CloudWatch enforces, so a malformed line fails here instead
of being dropped silently by CloudWatch (docs: "Specification: Embedded metric format").
"""

import json
from typing import Any

import pytest

UNITS = {"Milliseconds", "Seconds", "None"}  # the ones this repo uses


def check(record: dict[str, Any]) -> None:
    aws = record["_aws"]
    assert isinstance(aws["Timestamp"], int) and aws["Timestamp"] > 1_600_000_000_000  # ms
    [directive] = aws["CloudWatchMetrics"]
    assert directive["Namespace"] == "ragdp"
    for dimension_set in directive["Dimensions"]:
        assert len(dimension_set) <= 30
        for key in dimension_set:  # each dimension is a string member at the top level
            assert isinstance(record[key], str) and record[key].strip()
    assert 1 <= len(directive["Metrics"]) <= 100
    for definition in directive["Metrics"]:
        value = record[definition["Name"]]  # each value is a number at the top level
        assert isinstance(value, int | float) and not isinstance(value, bool)
        assert definition["Unit"] in UNITS


def emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """The EMF lines printed so far, each checked against the spec."""
    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"_aws"' in line
    ]
    for record in records:
        check(record)
    return records


def metric_names(record: dict[str, Any]) -> set[str]:
    return {m["Name"] for m in record["_aws"]["CloudWatchMetrics"][0]["Metrics"]}


def dimensions(record: dict[str, Any]) -> list[list[str]]:
    return record["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
