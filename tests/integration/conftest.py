"""Fixtures for integration tests.

These run against real engines rather than fakes. The legacy integration is
only meaningfully tested against actual SQL Server: a Postgres stand-in would
not exercise T-SQL dialect handling, the ODBC driver path, or the permission
model that ADR-005 depends on.

Tests skip rather than fail when the database is unreachable, so `poe test` on
a machine with no containers running still gives a useful signal.
"""

from __future__ import annotations

import pytest

from backend.app.config import get_settings
from backend.app.db.legacy.session import LegacyConnectionError, legacy_connection


def _legacy_available() -> tuple[bool, str]:
    try:
        with legacy_connection(readonly=True) as connection:
            connection.cursor().execute("SELECT 1").fetchone()
    except LegacyConnectionError as exc:
        return False, str(exc)
    except Exception as exc:  # driver-level failures surface as many types
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


@pytest.fixture(scope="session")
def legacy_ready() -> None:
    """Skip the suite unless the seeded legacy database is reachable."""
    available, reason = _legacy_available()
    if not available:
        pytest.skip(
            "Legacy SQL Server unavailable or unseeded "
            f"(run `poe up && poe seed`). Detail: {reason}"
        )


@pytest.fixture
def ro_cursor(legacy_ready: None):
    """A cursor on the restricted `axon_ai_ro` login."""
    with legacy_connection(readonly=True) as connection:
        yield connection.cursor()


@pytest.fixture
def settings():
    return get_settings()
