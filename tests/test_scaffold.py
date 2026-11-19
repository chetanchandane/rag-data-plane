"""Smoke test: the project layout and pytest config are wired up correctly."""

import sys


def test_python_version() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_service_packages_import() -> None:
    # Fails if pytest's pythonpath no longer points at services/.
    import api  # noqa: F401
    import shared  # noqa: F401
    import worker  # noqa: F401
