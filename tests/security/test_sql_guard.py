"""Adversarial tests for the SQL guard.

Claim C8 is that the AI's database access is provably read-only, with a target
of exactly zero prohibited operations. This suite is that proof, and CI treats
any failure as a hard gate rather than a regression to triage.

The benign cases at the end matter as much as the attacks. A validator that
blocks everything scores perfectly against attacks and is useless, so the
false-positive rate on legitimate queries is tested explicitly.
"""

from __future__ import annotations

import pytest

from backend.app.db.legacy.sqlguard import (
    ALLOWED_VIEWS,
    SqlGuardError,
    validate_read_only_sql,
)

pytestmark = [pytest.mark.security, pytest.mark.unit]


# ---------------------------------------------------------------------------
# Direct data-modifying statements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dbo.shipments",
        "DROP DATABASE AxonERP",
        "TRUNCATE TABLE dbo.shipments",
        "DELETE FROM dbo.shipments",
        "DELETE FROM vw_ai_shipments WHERE shipment_id = 'SH-2041'",
        "INSERT INTO dbo.shipments (shipment_id) VALUES ('SH-9999')",
        "UPDATE dbo.cargo_requirements SET permitted_temp_max_c = 99",
        "ALTER TABLE dbo.shipments ADD spare INT",
        "CREATE TABLE evil (id INT)",
        "CREATE VIEW sneaky AS SELECT * FROM dbo.customers",
        "MERGE dbo.shipments AS t USING dbo.shipments AS s ON 1=1",
        "GRANT SELECT ON dbo.customers TO axon_ai_ro",
    ],
)
def test_data_modifying_statements_are_rejected(sql: str):
    with pytest.raises(SqlGuardError):
        validate_read_only_sql(sql)


# ---------------------------------------------------------------------------
# Stacked statements and comment evasion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT shipment_id FROM vw_ai_shipments; DROP TABLE dbo.shipments",
        "SELECT 1 FROM vw_ai_shipments; DELETE FROM dbo.shipments",
        "SELECT shipment_id FROM vw_ai_shipments;;DROP TABLE dbo.shipments",
    ],
)
def test_stacked_statements_are_rejected(sql: str):
    """A second statement after a legitimate one is the classic injection."""
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql(sql)
    assert exc.value.reason in {"multiple_statements", "forbidden_node", "parse_failed"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT shipment_id FROM vw_ai_shipments -- ' OR 1=1",
        "SELECT shipment_id FROM vw_ai_shipments /* comment */ WHERE 1=1",
        "SELECT /*!DROP*/ shipment_id FROM vw_ai_shipments",
    ],
)
def test_comments_do_not_smuggle_anything_past_the_parser(sql: str):
    """Comments are stripped by the parser, so they cannot hide a verb.

    These are all legitimate SELECTs once parsed, which is the point: a string
    matcher would reject the third for containing "DROP".
    """
    guarded = validate_read_only_sql(sql)
    assert guarded.tables <= ALLOWED_VIEWS


# ---------------------------------------------------------------------------
# Writes hidden inside otherwise-valid queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1 AS n) INSERT INTO dbo.shipments SELECT * FROM x",
        "SELECT * INTO dbo.stolen FROM vw_ai_shipments",
        "SELECT shipment_id FROM vw_ai_shipments FOR UPDATE",
    ],
)
def test_writes_wrapped_in_query_syntax_are_rejected(sql: str):
    with pytest.raises(SqlGuardError):
        validate_read_only_sql(sql)


# ---------------------------------------------------------------------------
# Procedure execution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "EXEC xp_cmdshell 'dir'",
        "EXECUTE sp_executesql N'DROP TABLE dbo.shipments'",
        "EXEC sp_addsrvrolemember 'axon_ai_ro', 'sysadmin'",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'server=evil;', 'SELECT 1')",
    ],
)
def test_procedure_execution_is_rejected(sql: str):
    with pytest.raises(SqlGuardError):
        validate_read_only_sql(sql)


# ---------------------------------------------------------------------------
# Unauthorised relations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dbo.shipments",
        "SELECT * FROM customers",
        "SELECT contract_rate_usd FROM dbo.customers",
        "SELECT phone FROM dbo.drivers",
        "SELECT * FROM AxonERP.dbo.shipments",
        "SELECT * FROM sys.database_principals",
        "SELECT * FROM INFORMATION_SCHEMA.TABLES",
    ],
)
def test_base_tables_and_system_views_are_rejected(sql: str):
    """Qualifying a table name must not defeat the check."""
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql(sql)
    assert exc.value.reason == "forbidden_table"


def test_a_join_to_a_forbidden_table_is_rejected():
    """One approved view does not launder a forbidden join partner."""
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql(
            "SELECT s.shipment_id, c.contract_rate_usd "
            "FROM vw_ai_shipments s JOIN dbo.customers c "
            "ON c.customer_id = s.customer_id"
        )
    assert exc.value.reason == "forbidden_table"


def test_a_forbidden_table_in_a_subquery_is_rejected():
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql(
            "SELECT shipment_id FROM vw_ai_shipments WHERE customer_id IN "
            "(SELECT customer_id FROM dbo.customers)"
        )
    assert exc.value.reason == "forbidden_table"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
def test_empty_input_is_rejected(sql: str):
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql(sql)
    assert exc.value.reason == "empty_statement"


def test_a_query_reading_nothing_is_rejected():
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql("SELECT 1")
    assert exc.value.reason == "no_table_referenced"


# ---------------------------------------------------------------------------
# Resource bounds
# ---------------------------------------------------------------------------


def test_an_unbounded_query_gets_a_row_limit():
    guarded = validate_read_only_sql("SELECT * FROM vw_ai_shipments", max_rows=500)
    assert guarded.row_limit == 500
    assert "500" in guarded.sql


def test_a_query_asking_for_more_than_the_cap_is_reduced():
    guarded = validate_read_only_sql("SELECT * FROM vw_ai_shipments LIMIT 100000", max_rows=500)
    assert guarded.row_limit == 500


def test_a_query_asking_for_less_than_the_cap_keeps_its_limit():
    guarded = validate_read_only_sql("SELECT * FROM vw_ai_shipments LIMIT 10", max_rows=500)
    assert guarded.row_limit == 10


# ---------------------------------------------------------------------------
# Benign queries must NOT be blocked
#
# A validator that rejects everything passes every attack test above and is
# worthless. These are the false-positive controls.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM vw_ai_shipments",
        "SELECT shipment_id, cargo_value_usd FROM vw_ai_shipments WHERE status = 'in_transit'",
        "SELECT COUNT(*) FROM vw_ai_shipments",
        "SELECT customer_tier, COUNT(*) FROM vw_ai_shipments GROUP BY customer_tier",
        "SELECT customer_tier, AVG(cargo_value_usd) FROM vw_ai_shipments "
        "GROUP BY customer_tier HAVING COUNT(*) > 1",
        "SELECT shipment_id FROM vw_ai_shipments ORDER BY cargo_value_usd DESC",
        "SELECT s.shipment_id, c.permitted_temp_max_c FROM vw_ai_shipments s "
        "JOIN vw_ai_cargo_requirements c ON c.shipment_id = s.shipment_id",
        "SELECT vehicle_id, fault_codes FROM vw_ai_maintenance WHERE fault_codes LIKE '%AL17%'",
        "SELECT facility_id FROM vw_ai_facilities WHERE slots_available > 0",
        "WITH recent AS (SELECT * FROM vw_ai_maintenance WHERE days_ago < 90) "
        "SELECT vehicle_id, COUNT(*) FROM recent GROUP BY vehicle_id",
        "SELECT shipment_id, CASE WHEN cargo_value_usd > 100000 THEN 'high' ELSE 'low' END "
        "FROM vw_ai_shipments",
        "SELECT DISTINCT root_cause FROM vw_ai_historical_incidents",
    ],
)
def test_legitimate_queries_are_permitted(sql: str):
    guarded = validate_read_only_sql(sql)
    assert guarded.tables <= ALLOWED_VIEWS
    assert guarded.sql


def test_a_customer_name_containing_a_keyword_is_not_blocked():
    """The exact case a string-matching guard gets wrong.

    "DROP Logistics" is a legitimate customer name. A guard that searches for
    the substring "DROP" would reject this query and be quietly removed by the
    first engineer it inconveniences.
    """
    guarded = validate_read_only_sql(
        "SELECT shipment_id FROM vw_ai_shipments WHERE customer_name = 'DROP Logistics'"
    )
    assert guarded.tables == {"vw_ai_shipments"}


def test_a_note_mentioning_delete_is_not_blocked():
    guarded = validate_read_only_sql(
        "SELECT event_id FROM vw_ai_maintenance "
        "WHERE technician_notes LIKE '%delete the old filter%'"
    )
    assert guarded.tables == {"vw_ai_maintenance"}


# ---------------------------------------------------------------------------
# Rejections are attributable
# ---------------------------------------------------------------------------


def test_rejections_carry_a_machine_readable_reason():
    """Rejections are counted by category in metrics, not just logged."""
    with pytest.raises(SqlGuardError) as exc:
        validate_read_only_sql("DROP TABLE dbo.shipments")
    assert exc.value.reason
    assert exc.value.detail


def test_allowed_views_matches_the_documented_surface():
    """Drift between this set and the grants would open a gap silently."""
    assert {
        "vw_ai_shipments",
        "vw_ai_vehicles",
        "vw_ai_cargo_requirements",
        "vw_ai_maintenance",
        "vw_ai_facilities",
        "vw_ai_historical_incidents",
    } == ALLOWED_VIEWS
