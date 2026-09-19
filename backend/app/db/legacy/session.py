"""Connection handling for the legacy SQL Server.

Two things live here that are easy to get wrong and expensive to debug.

**Driver resolution.** ODBC driver availability varies by machine: CI installs
`ODBC Driver 18`, a developer laptop may only have the ancient `SQL Server`
driver that ships with Windows, and a container image may have 17. Hard-coding
one produces the unhelpful "Data source name not found" error on every machine
that does not have it. We resolve against what is actually installed, in
preference order, and say clearly what we found if nothing matches.

**Synchronous driver, asynchronous application.** There is no mature async
ODBC driver, so every legacy read runs in a worker thread. That is why this is
the only module permitted to import `pyodbc` (enforced by an import-linter
contract): the boundary keeps blocking I/O in one place instead of letting it
leak into the request path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pyodbc

from backend.app.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "LegacyConnectionError",
    "legacy_connection",
    "resolve_driver",
    "run_in_thread",
]

#: Preference order. Newer drivers support TLS properly and are what CI and
#: production use; the bare "SQL Server" driver is a last-resort fallback that
#: ships with Windows and is good enough for local development.
_DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server",
)

#: Drivers that understand TrustServerCertificate. The legacy driver rejects
#: the keyword outright rather than ignoring it, so it must be omitted there.
_SUPPORTS_TRUST_CERT = frozenset(
    {
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
    }
)


class LegacyConnectionError(RuntimeError):
    """Raised when the legacy system cannot be reached or configured."""


@lru_cache(maxsize=1)
def resolve_driver(preferred: str | None = None) -> str:
    """Pick the best installed ODBC driver.

    A configured `preferred` driver wins if it is actually installed.
    Otherwise the highest-preference available driver is used, so a machine
    without the modern driver still works rather than failing obscurely.
    """
    installed = set(pyodbc.drivers())
    if not installed:
        raise LegacyConnectionError(
            "No ODBC drivers are installed. Install the Microsoft ODBC Driver "
            "for SQL Server (msodbcsql18) to reach the legacy system."
        )

    if preferred and preferred in installed:
        return preferred

    for candidate in _DRIVER_PREFERENCE:
        if candidate in installed:
            return candidate

    raise LegacyConnectionError(
        f"No supported SQL Server ODBC driver found. Installed drivers: "
        f"{sorted(installed)}. Expected one of {list(_DRIVER_PREFERENCE)}."
    )


def build_connection_string(
    settings: Settings,
    *,
    readonly: bool = True,
    database: str | None = None,
) -> str:
    """Assemble an ODBC connection string for the resolved driver.

    `readonly=True` uses the restricted `axon_ai_ro` login, which holds SELECT
    on six views and nothing else. Reaching `sa` requires an explicit
    `readonly=False`, which only the seed script passes.
    """
    driver = resolve_driver(settings.mssql_driver)
    user = settings.mssql_ro_user if readonly else "sa"
    password = (
        settings.mssql_ro_password if readonly else settings.mssql_sa_password
    ).get_secret_value()

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={settings.mssql_host},{settings.mssql_port}",
        f"DATABASE={database or settings.mssql_db}",
        f"UID={user}",
        f"PWD={password}",
        f"Connection Timeout={settings.mssql_query_timeout_seconds}",
    ]
    if driver in _SUPPORTS_TRUST_CERT and settings.mssql_trust_cert:
        parts.append("TrustServerCertificate=yes")

    return ";".join(parts)


@contextmanager
def legacy_connection(
    *,
    readonly: bool = True,
    database: str | None = None,
    settings: Settings | None = None,
    autocommit: bool = False,
) -> Iterator[pyodbc.Connection]:
    """Open a connection to the legacy system.

    The statement timeout is applied on every connection, not only on queries
    the guard has seen. It is the third layer of protection alongside the
    read-only grant and the AST allowlist: a pathological query cannot degrade
    the ERP for the humans who depend on it.
    """
    resolved = settings or get_settings()
    connection_string = build_connection_string(resolved, readonly=readonly, database=database)

    try:
        connection = pyodbc.connect(
            connection_string,
            timeout=resolved.mssql_query_timeout_seconds,
            autocommit=autocommit,
        )
    except pyodbc.Error as exc:
        # The connection string carries a password, so it must never appear in
        # an exception message or a log line.
        raise LegacyConnectionError(
            f"Could not connect to the legacy system at "
            f"{resolved.mssql_host}:{resolved.mssql_port}: {exc.args[0]}"
        ) from exc

    connection.timeout = resolved.mssql_query_timeout_seconds
    try:
        yield connection
    finally:
        connection.close()


async def run_in_thread[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking legacy call in a worker thread.

    Every legacy read goes through here. pyodbc is synchronous and there is no
    mature async ODBC driver, so the alternative would be blocking the event
    loop on a database the application does not control.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
