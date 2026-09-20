"""Fixtures for the end-to-end suite.

The demo builds its own engines and owns its own transaction, exactly as the
application does, so this suite needs no session fixtures - only the checks
that the two real engines are there at all. Re-exported from the integration
suite rather than duplicated, because two definitions would eventually
disagree about which role the application connects as.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    app_db_ready,
    app_engine,
    legacy_ready,
    owner_engine,
    settings,
)
