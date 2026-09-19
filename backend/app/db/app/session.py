"""Async engine and session factory for the application database.

Separate from the legacy path on purpose, and kept separate by an
import-linter contract. The legacy system is synchronous pyodbc against SQL
Server and is read-only; this is asyncpg against PostgreSQL and is the half of
the world we own. Mixing the two connection models is how blocking I/O ends up
on the event loop.

**Which role connects.** The application runs as `postgres_app_user`, not as
the schema owner. That is not tidiness: PostgreSQL does not apply table
privileges to superusers, so the `REVOKE UPDATE, DELETE ON audit_event` in
migration 0002 protects nothing at all if the application shares the owner's
role. The owner DSN exists for Alembic and for tests that need to act as an
attacker with elevated access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.config import Settings, get_settings

__all__ = [
    "build_engine",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "session_scope",
]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def build_engine(
    dsn: str | None = None,
    *,
    settings: Settings | None = None,
    owner: bool = False,
) -> AsyncEngine:
    """Create an engine. Callers that want the process-wide one use `get_engine`.

    `owner=True` connects as the schema owner, which bypasses the append-only
    grants. Only migrations and tests that deliberately simulate privileged
    access should pass it.
    """
    resolved = settings or get_settings()
    url = dsn or (resolved.postgres_dsn if owner else resolved.postgres_app_dsn)
    return create_async_engine(
        url,
        pool_size=resolved.postgres_pool_size,
        max_overflow=resolved.postgres_max_overflow,
        # Recycle below any reasonable server or proxy idle timeout. A stale
        # pooled connection surfaces as a confusing mid-incident failure.
        pool_recycle=1800,
        pool_pre_ping=True,
        connect_args={
            "command_timeout": resolved.postgres_command_timeout_seconds,
            # asyncpg caches prepared statements per connection, which breaks
            # against a pooler in transaction mode and after a migration
            # changes a table's shape underneath a live connection.
            "statement_cache_size": 0,
        },
    )


def get_engine() -> AsyncEngine:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory.

    `expire_on_commit=False` because the alternative is that every attribute
    read after a commit issues another query - including inside a FastAPI
    response serializer, where the session may already be closed.
    """
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A session wrapped in a transaction that commits or rolls back.

    The rollback is explicit rather than left to session teardown: an
    exception mid-incident must not leave a half-written incident behind for
    the next detector run to find.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    """Close the pool. Called on application shutdown and between tests."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
