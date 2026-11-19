"""Unit tests for PermanentError (no network)."""

from shared.errors import MAX_REASON_LENGTH, PermanentError


def test_carries_a_code_and_a_message() -> None:
    error = PermanentError("sha256_mismatch", "file hash differs from the registered hash")
    assert error.code == "sha256_mismatch"
    assert str(error) == "file hash differs from the registered hash"
    assert error.reason == "sha256_mismatch: file hash differs from the registered hash"


def test_reason_is_capped() -> None:
    error = PermanentError("unparseable_pdf", "x" * 1000)
    assert len(error.reason) == MAX_REASON_LENGTH
    assert error.reason.startswith("unparseable_pdf: ")


def test_only_permanent_errors_are_permanent() -> None:
    # The handler's rule is "PermanentError or not". Ordinary exceptions (timeouts,
    # throttling, bugs) are not PermanentError, so they stay transient by default.
    assert not issubclass(TimeoutError, PermanentError)
    assert not issubclass(ConnectionError, PermanentError)
    assert not issubclass(PermanentError, (TimeoutError, ConnectionError, ValueError))
