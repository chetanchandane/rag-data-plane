"""Unit tests for tracing setup (shared/telemetry.py). No network: the exporter is a fake
and the global provider is never replaced."""

import logging

import pytest

from shared import telemetry


class FakeSsm:
    def __init__(self, value: str = "key-from-ssm", error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls: list[dict] = []

    def get_parameter(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


class FakeExporter:
    created: list["FakeExporter"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        FakeExporter.created.append(self)

    def export(self, spans: object) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records providers setup_tracing would install, instead of replacing the global one,
    and swaps the OTLP exporter for a fake."""
    providers: list[object] = []
    FakeExporter.created.clear()
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", providers.append)
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", FakeExporter)
    return providers


def test_no_key_leaves_tracing_off(installed: list[object]) -> None:
    assert telemetry.setup_tracing("svc", {}) is None
    assert installed == []


def test_the_key_comes_from_the_environment_locally() -> None:
    assert telemetry.honeycomb_api_key({"HONEYCOMB_API_KEY": "abc"}) == "abc"


def test_on_lambda_the_key_comes_from_ssm_decrypted() -> None:
    ssm = FakeSsm()
    key = telemetry.honeycomb_api_key({"HONEYCOMB_API_KEY_PARAM": "/ragdp/prod/k"}, ssm)
    assert key == "key-from-ssm"
    assert ssm.calls == [{"Name": "/ragdp/prod/k", "WithDecryption": True}]


def test_setup_sends_to_honeycomb_with_the_key_header(installed: list[object]) -> None:
    provider = telemetry.setup_tracing("ragdp-worker", {"HONEYCOMB_API_KEY": "abc"})
    assert installed == [provider]
    assert provider.resource.attributes["service.name"] == "ragdp-worker"
    [exporter] = FakeExporter.created
    assert exporter.kwargs["endpoint"] == "https://api.honeycomb.io/v1/traces"
    assert exporter.kwargs["headers"] == {"x-honeycomb-team": "abc"}


def test_the_endpoint_can_be_overridden(installed: list[object]) -> None:
    environ = {"HONEYCOMB_API_KEY": "abc", "HONEYCOMB_ENDPOINT": "https://api.eu1.honeycomb.io/"}
    assert telemetry.setup_tracing("svc", environ) is not None
    [exporter] = FakeExporter.created
    assert exporter.kwargs["endpoint"] == "https://api.eu1.honeycomb.io/v1/traces"


@pytest.mark.parametrize(
    ("environ", "ssm"),
    [
        # Both sources set: ambiguous, so refused.
        ({"HONEYCOMB_API_KEY": "a", "HONEYCOMB_API_KEY_PARAM": "/p"}, FakeSsm()),
        # SSM unreachable or access denied.
        ({"HONEYCOMB_API_KEY_PARAM": "/p"}, FakeSsm(error=RuntimeError("AccessDenied"))),
    ],
)
def test_a_setup_failure_turns_tracing_off_instead_of_raising(
    installed: list[object], environ: dict, ssm: FakeSsm, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="shared.telemetry"):
        assert telemetry.setup_tracing("svc", environ, ssm) is None
    assert installed == []
    assert "tracing off: setup failed" in caplog.text


class FakeProvider:
    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.timeouts: list[int] = []

    def force_flush(self, timeout_millis: int) -> bool:
        self.timeouts.append(timeout_millis)
        if self.error:
            raise self.error
        return self.result


def test_flush_waits_a_bounded_time() -> None:
    provider = FakeProvider()
    telemetry.flush(provider)
    assert provider.timeouts == [telemetry.FLUSH_TIMEOUT_MILLIS]


def test_flush_without_tracing_does_nothing() -> None:
    telemetry.flush(None)


@pytest.mark.parametrize(
    "provider", [FakeProvider(result=False), FakeProvider(error=RuntimeError("network down"))]
)
def test_a_failed_flush_is_logged_not_raised(
    provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="shared.telemetry"):
        telemetry.flush(provider)
    assert "tracing:" in caplog.text
