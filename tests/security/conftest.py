"""Database fixtures for the security suite.

Re-exported from the integration suite rather than duplicated. The I5 gate is
a claim about what ends up *persisted* after an attack, so it needs the same
real database the integration tests use - and two definitions of the same
fixture would eventually disagree about which role the application connects
as, which is the one detail these tests depend on most.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    app_db_ready,
    app_engine,
    app_session,
    owner_engine,
    settings,
)
