"""Seed the legacy SQL Server with the Axon ERP schema, data, views and grants.

Run with: uv run poe seed

Idempotent. Re-running drops and recreates the schema, so a developer can
reset to a known state without recreating the container.

The scripts are applied with the `sa` login because they create objects and a
login. Everything the application does afterwards uses `axon_ai_ro`, which can
only SELECT from six views. That asymmetry is the whole point, and the
verification step at the end proves it holds.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pyodbc

from backend.app.config import get_settings
from backend.app.db.legacy.session import (
    LegacyConnectionError,
    legacy_connection,
    resolve_driver,
)

SEED_DIR = Path(__file__).resolve().parents[1] / "data" / "seed" / "legacy"

#: T-SQL batch separator. pyodbc submits one statement at a time, so scripts
#: must be split on GO exactly as sqlcmd would.
_GO = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)


def split_batches(script: str) -> list[str]:
    return [batch.strip() for batch in _GO.split(script) if batch.strip()]


def apply_script(path: Path, *, database: str | None) -> int:
    settings = get_settings()
    script = path.read_text(encoding="utf-8")

    # The read-only login's password lives in configuration, never in source.
    script = script.replace("{{RO_PASSWORD}}", settings.mssql_ro_password.get_secret_value())

    batches = split_batches(script)
    with legacy_connection(readonly=False, database=database, autocommit=True) as connection:
        cursor = connection.cursor()
        for index, batch in enumerate(batches, start=1):
            try:
                cursor.execute(batch)
                # Some batches return rows; drain them so the next execute works.
                while cursor.nextset():
                    pass
            except pyodbc.Error as exc:
                preview = batch.splitlines()[0][:90]
                print(
                    f"  FAILED batch {index}/{len(batches)}: {preview}\n    {exc.args[-1]}",
                    file=sys.stderr,
                )
                raise
    return len(batches)


def verify_least_privilege() -> list[str]:
    """Prove the restricted login is actually restricted.

    Seeding is not finished until this passes. A grant script that silently
    failed would leave the system wide open while every other check looked fine.
    """
    problems: list[str] = []

    with legacy_connection(readonly=True) as connection:
        cursor = connection.cursor()

        # It must be able to read every approved view.
        for view in (
            "vw_ai_shipments",
            "vw_ai_vehicles",
            "vw_ai_cargo_requirements",
            "vw_ai_maintenance",
            "vw_ai_facilities",
            "vw_ai_historical_incidents",
        ):
            try:
                # never from user input. Identifiers cannot be parameterised.
                cursor.execute(f"SELECT TOP 1 * FROM dbo.{view}")  # noqa: S608
                cursor.fetchall()
            except pyodbc.Error as exc:
                problems.append(f"cannot read approved view {view}: {exc.args[-1]}")

        # It must not be able to read base tables.
        for table in ("shipments", "customers", "drivers"):
            try:
                cursor.execute(f"SELECT TOP 1 * FROM dbo.{table}")  # noqa: S608
                cursor.fetchall()
            except pyodbc.Error:
                pass  # expected
            else:
                problems.append(f"CAN READ base table {table} - grants are wrong")

        # It must not be able to write anything.
        for statement, label in (
            ("INSERT INTO dbo.shipments (shipment_id) VALUES ('SH-X')", "INSERT"),
            ("UPDATE dbo.shipments SET status = 'x'", "UPDATE"),
            ("DELETE FROM dbo.shipments", "DELETE"),
        ):
            try:
                cursor.execute(statement)
            except pyodbc.Error:
                pass  # expected
            else:
                problems.append(f"CAN {label} - grants are wrong")

    return problems


def main() -> int:
    settings = get_settings()

    try:
        driver = resolve_driver(settings.mssql_driver)
    except LegacyConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Seeding legacy ERP at {settings.mssql_host}:{settings.mssql_port}")
    print(f"  driver: {driver}\n")

    scripts = sorted(SEED_DIR.glob("*.sql"))
    if not scripts:
        print(f"ERROR: no SQL scripts found in {SEED_DIR}", file=sys.stderr)
        return 1

    for path in scripts:
        # The first script creates the database, so it must connect to master.
        database = "master" if path.name.startswith("01_") else settings.mssql_db
        try:
            count = apply_script(path, database=database)
        except (pyodbc.Error, LegacyConnectionError) as exc:
            print(f"\nERROR applying {path.name}: {exc}", file=sys.stderr)
            return 1
        print(f"  applied {path.name}  ({count} batches)")

    print("\nVerifying least privilege...")
    problems = verify_least_privilege()
    if problems:
        print("\nFAILED - the restricted login is not restricted:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("  axon_ai_ro can read the six approved views")
    print("  axon_ai_ro cannot read base tables")
    print("  axon_ai_ro cannot INSERT, UPDATE or DELETE")
    print("\nLegacy ERP seeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
