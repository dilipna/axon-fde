"""Integration tests for legacy SQL Server access.

This is the executable form of ADR-005's verification checklist, and it is the
evidence behind claim C8. The claim is that the AI's database access is
provably read-only; these tests run against a real SQL Server and prove it at
the permission layer, independently of the AST guard.

Both layers are tested separately on purpose. The guard is tested in
`tests/security/test_sql_guard.py` with no database at all. Here the guard is
bypassed entirely and the statements go straight to the driver, so a failure
of one layer cannot mask a failure of the other.
"""

from __future__ import annotations

import pyodbc
import pytest

from backend.app.db.legacy.session import legacy_connection

pytestmark = [pytest.mark.integration, pytest.mark.security]

APPROVED_VIEWS = [
    "vw_ai_shipments",
    "vw_ai_vehicles",
    "vw_ai_cargo_requirements",
    "vw_ai_maintenance",
    "vw_ai_facilities",
    "vw_ai_historical_incidents",
]

BASE_TABLES = [
    "shipments",
    "customers",
    "drivers",
    "vehicles",
    "cargo_requirements",
    "maintenance_events",
    "historical_incidents",
    "facilities",
    "routes",
]


# ---------------------------------------------------------------------------
# The restricted login can read exactly what it should
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", APPROVED_VIEWS)
def test_restricted_login_can_read_each_approved_view(ro_cursor, view: str):
    rows = ro_cursor.execute(f"SELECT TOP 5 * FROM dbo.{view}").fetchall()
    assert rows, f"{view} returned no rows; is the database seeded?"


# ---------------------------------------------------------------------------
# ...and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", BASE_TABLES)
def test_restricted_login_cannot_read_base_tables(ro_cursor, table: str):
    """The property that makes the exposure surface enumerable.

    Six views, explicit columns. A new column on a base table is invisible
    until someone deliberately adds it to a view.
    """
    with pytest.raises(pyodbc.Error):
        ro_cursor.execute(f"SELECT TOP 1 * FROM dbo.{table}").fetchall()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO dbo.shipments (shipment_id) VALUES ('SH-EVIL')",
        "UPDATE dbo.cargo_requirements SET permitted_temp_max_c = 99",
        "DELETE FROM dbo.shipments",
        "TRUNCATE TABLE dbo.shipments",
        "DROP TABLE dbo.shipments",
        "ALTER TABLE dbo.shipments ADD spare INT",
        "CREATE TABLE dbo.evil (id INT)",
        "UPDATE dbo.vw_ai_shipments SET status = 'x'",
        "DELETE FROM dbo.vw_ai_shipments",
    ],
)
def test_restricted_login_cannot_write_anything(ro_cursor, statement: str):
    """Writes fail at the permission layer, with the AST guard bypassed.

    Even the approved views cannot be written through, which matters because
    a view is otherwise updatable in SQL Server.
    """
    with pytest.raises(pyodbc.Error):
        ro_cursor.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "EXEC xp_cmdshell 'dir'",
        "EXEC sp_addsrvrolemember 'axon_ai_ro', 'sysadmin'",
    ],
)
def test_restricted_login_cannot_execute_procedures(ro_cursor, statement: str):
    with pytest.raises(pyodbc.Error):
        ro_cursor.execute(statement)


def test_restricted_login_cannot_escalate_its_own_privileges(legacy_ready):
    """Asserts the *outcome* of an escalation attempt, not that it errors.

    This test originally expected `pyodbc.Error`, and it failed: the legacy
    "SQL Server" ODBC driver does not surface a failed GRANT as an exception,
    so the statement appeared to succeed. The escalation had no effect - the
    read below is still denied - but the test was checking the mechanism
    rather than the result.

    Security tests should assert what an attacker ends up able to do, not that
    a particular error type was raised on the way. A driver that silently
    swallows the failure must not turn into a green test.
    """
    with legacy_connection(readonly=True) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("GRANT SELECT ON dbo.customers TO axon_ai_ro")
            connection.commit()
        except pyodbc.Error:
            pass  # Some drivers do raise. Either way, what matters is below.

    # The only thing that counts: the privilege was not actually acquired.
    with legacy_connection(readonly=True) as connection, pytest.raises(pyodbc.Error):
        connection.cursor().execute("SELECT TOP 1 * FROM dbo.customers").fetchall()


def test_write_attempts_have_no_effect_even_if_the_driver_stays_quiet(ro_cursor):
    """Belt and braces for the same driver quirk, on the write path.

    The write tests above assert that an exception is raised, which holds for
    every driver tested so far. This asserts the outcome independently, so a
    driver that swallowed a failed DELETE could not make the suite pass while
    the data was actually being modified.
    """
    before = ro_cursor.execute("SELECT COUNT(*) FROM dbo.vw_ai_shipments").fetchone()[0]

    with legacy_connection(readonly=True) as connection:
        cursor = connection.cursor()
        for statement in (
            "DELETE FROM dbo.shipments",
            "UPDATE dbo.shipments SET status = 'tampered'",
        ):
            try:
                cursor.execute(statement)
                connection.commit()
            except pyodbc.Error:
                pass

    after = ro_cursor.execute("SELECT COUNT(*) FROM dbo.vw_ai_shipments").fetchone()[0]
    assert after == before, "rows were modified through the read-only login"

    tampered = ro_cursor.execute(
        "SELECT COUNT(*) FROM dbo.vw_ai_shipments WHERE status = 'tampered'"
    ).fetchone()[0]
    assert tampered == 0


# ---------------------------------------------------------------------------
# Sensitive columns never leave the database
# ---------------------------------------------------------------------------


def test_views_exclude_commercially_sensitive_pricing(ro_cursor):
    """Contract rates are excluded at the database, not filtered downstream.

    Filtering after retrieval would mean the value had already entered the
    application, one mistake away from a context bundle.
    """
    columns = {
        d[0].lower()
        for d in ro_cursor.execute("SELECT TOP 1 * FROM dbo.vw_ai_shipments").description
    }
    assert "contract_rate_usd" not in columns


def test_views_exclude_driver_personal_data(ro_cursor):
    columns = {
        d[0].lower()
        for d in ro_cursor.execute("SELECT TOP 1 * FROM dbo.vw_ai_shipments").description
    }
    assert "phone" not in columns
    assert "home_address" not in columns
    # The name is present: a dispatcher needs to know who is driving.
    assert "driver_name" in columns


# ---------------------------------------------------------------------------
# The seeded data supports the flagship scenario
# ---------------------------------------------------------------------------


def test_flagship_shipment_exists_with_its_erp_envelope(ro_cursor):
    """SH-2041's ERP record says 2-10 C.

    The signed Bill of Lading says 2-8 C. Nothing in Axon's current process
    reconciles them, which is the conflict the evidence engine detects.
    """
    row = ro_cursor.execute(
        "SELECT permitted_temp_min_c, permitted_temp_max_c "
        "FROM dbo.vw_ai_cargo_requirements WHERE shipment_id = 'SH-2041'"
    ).fetchone()

    assert row is not None
    assert float(row[0]) == 2.0
    assert float(row[1]) == 10.0, "the ERP/BOL mismatch is the flagship conflict"


def test_flagship_vehicle_has_corroborating_fault_history(ro_cursor):
    """AX-042 has prior AL17 warnings, so the code the simulation raises has
    history behind it rather than appearing from nowhere."""
    rows = ro_cursor.execute(
        "SELECT event_id, fault_codes, severity FROM dbo.vw_ai_maintenance "
        "WHERE vehicle_id = 'AX-042' AND fault_codes LIKE '%AL17%'"
    ).fetchall()
    assert len(rows) >= 2


def test_cold_storage_facilities_are_available_for_reroute(ro_cursor):
    """At least one approved facility must have capacity, or the flagship
    scenario has no feasible intervention to recommend."""
    rows = ro_cursor.execute(
        "SELECT facility_id, slots_available FROM dbo.vw_ai_facilities "
        "WHERE slots_available > 0 AND capabilities LIKE '%pharma_certified%'"
    ).fetchall()
    assert rows, "no pharma-certified facility has capacity"


def test_historical_incidents_cover_the_expected_root_causes(ro_cursor):
    """Institutional memory needs prior cases spanning several causes."""
    causes = {
        row[0]
        for row in ro_cursor.execute(
            "SELECT DISTINCT root_cause FROM dbo.vw_ai_historical_incidents"
        ).fetchall()
    }
    assert {"compressor_degradation", "sensor_malfunction"} <= causes


# ---------------------------------------------------------------------------
# Resource bounds
# ---------------------------------------------------------------------------


def test_connection_carries_a_statement_timeout(legacy_ready, settings):
    """The third layer alongside the grant and the AST guard: a pathological
    query cannot degrade the ERP for the humans who depend on it."""
    with legacy_connection(readonly=True) as connection:
        assert connection.timeout == settings.mssql_query_timeout_seconds
        assert connection.timeout > 0


def test_sa_login_is_reachable_only_when_explicitly_requested(settings):
    """Read-only is the default; reaching `sa` must be a deliberate act."""
    from backend.app.db.legacy.session import build_connection_string

    assert f"UID={settings.mssql_ro_user}" in build_connection_string(settings)
    assert "UID=sa" in build_connection_string(settings, readonly=False)
