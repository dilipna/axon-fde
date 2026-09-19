"""Fixtures for integration tests.

These run against real engines rather than fakes. The legacy integration is
only meaningfully tested against actual SQL Server: a Postgres stand-in would
not exercise T-SQL dialect handling, the ODBC driver path, or the permission
model that ADR-005 depends on.

Tests skip rather than fail when the database is unreachable, so `poe test` on
a machine with no containers running still gives a useful signal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.config import get_settings
from backend.app.db.app.models import AuditEvent, Base
from backend.app.db.app.session import build_engine
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


# ---------------------------------------------------------------------------
# Application database
# ---------------------------------------------------------------------------
#
# Two engines, because the append-only guarantee is a claim about *roles*.
# Testing it from the owner's connection would prove nothing: PostgreSQL does
# not apply table privileges to superusers, so the REVOKE would appear to work
# only because the trigger caught it. The tests need both connections to tell
# those two layers apart.


async def _probe_app_db() -> tuple[bool, str]:
    engine = build_engine()
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.select(sa.func.count()).select_from(AuditEvent))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        await engine.dispose()
    return True, ""


@pytest.fixture(scope="session")
def app_db_ready() -> None:
    """Skip unless the migrated application database is reachable.

    Checks the restricted runtime role specifically. A database that is up but
    unmigrated fails this too, which is the intent: every test below depends
    on migration 0002 having run.
    """
    available, reason = asyncio.run(_probe_app_db())
    if not available:
        pytest.skip(
            f"Application database unavailable or unmigrated "
            f"(run `poe up && poe migrate`). Detail: {reason}"
        )


async def _truncate_all(engine: AsyncEngine) -> None:
    """Empty every table between tests.

    TRUNCATE rather than DELETE because DELETE on `audit_event` is refused by
    the trigger - which is the point of the table. This runs as the owner, who
    holds TRUNCATE; the runtime role does not, precisely so that this escape
    hatch is not reachable from application code.
    """
    tables = ", ".join(sorted(Base.metadata.tables))
    async with engine.begin() as connection:
        await connection.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def owner_engine(app_db_ready: None) -> AsyncIterator[AsyncEngine]:
    """An engine on the schema owner: a stand-in for privileged access.

    Engines are built per test rather than shared. A pooled asyncpg connection
    belongs to the event loop that created it, and reusing one across the
    per-test loops pytest-asyncio creates fails in ways that look like
    database faults.
    """
    engine = build_engine(owner=True)
    await _truncate_all(engine)
    try:
        yield engine
    finally:
        await _truncate_all(engine)
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(owner_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """An engine on the restricted runtime role. This is how the app connects."""
    engine = build_engine()
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_session(app_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(app_engine, expire_on_commit=False)() as session:
        yield session


@pytest_asyncio.fixture
async def owner_session(owner_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(owner_engine, expire_on_commit=False)() as session:
        yield session


#: A fixed key, so a test that writes a keyed chain and one that verifies it
#: agree. Never a default anywhere in the application.
TEST_HMAC_KEY = b"test-audit-key-not-used-anywhere-else"
