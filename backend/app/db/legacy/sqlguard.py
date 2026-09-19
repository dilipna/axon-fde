"""Structural validation of SQL before it reaches the driver.

The read-only grant (ADR-005) already removes the privilege to do damage. This
is the second independent layer, and it exists for three reasons:

1. **Defence in depth.** The grant is configuration. Configuration drifts, is
   restored from the wrong backup, or is applied differently in a new
   environment. A control that depends on exactly one correctly-applied grant
   is one mistake away from nothing.
2. **The grant does not cover everything.** A syntactically valid SELECT can
   still be an unbounded cross join that degrades the ERP for the humans who
   depend on it.
3. **Observability.** A rejection with a structural reason is a security
   signal we can count and attribute. A permission error from the database
   arrives later, with less context.

Validation is an **allowlist over a parsed syntax tree**, never string
matching. `"DROP" in sql.upper()` rejects a legitimate query about a customer
named "DROP Logistics" and accepts `SEL/**/ECT`. Regex SQL guards are theatre.

See docs/adr/006-sql-parsed-before-execution.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

__all__ = [
    "ALLOWED_VIEWS",
    "SqlGuardError",
    "validate_read_only_sql",
]

DIALECT = "tsql"

#: The only relations any generated query may reference. Matches the grants in
#: data/seed/legacy/04_grants.sql exactly.
ALLOWED_VIEWS = frozenset(
    {
        "vw_ai_shipments",
        "vw_ai_vehicles",
        "vw_ai_cargo_requirements",
        "vw_ai_maintenance",
        "vw_ai_facilities",
        "vw_ai_historical_incidents",
    }
)

#: Expression types permitted anywhere in the tree.
#:
#: An allowlist rather than a denylist: anything not named here is rejected,
#: including node types introduced by a future SQLGlot version. A denylist
#: would need updating every time the grammar grows, and would fail open.
_ALLOWED_NODES: tuple[type[exp.Expression], ...] = (
    # Query shape
    exp.Select,
    exp.From,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.Join,
    exp.Subquery,
    exp.With,
    exp.CTE,
    exp.TableAlias,
    exp.Table,
    exp.Alias,
    exp.Distinct,
    exp.Star,
    exp.Column,
    exp.Identifier,
    exp.Schema,
    # Literals and types
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.DataType,
    exp.Cast,
    # Predicates and operators
    exp.And,
    exp.Or,
    exp.Not,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Is,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
    exp.Paren,
    exp.Neg,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    # The abstract bases Condition, Binary, Unary and Predicate are
    # deliberately NOT listed. Including them would admit every concrete
    # subclass, including ones added by a future SQLGlot version, which is
    # exactly the fail-open behaviour an allowlist exists to prevent.
    # Aggregates and common scalars
    exp.Count,
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Abs,
    exp.Round,
    exp.Coalesce,
    exp.Case,
    exp.If,
    exp.Lower,
    exp.Upper,
    exp.Length,
    exp.Substring,
    exp.Trim,
    exp.Concat,
    exp.CurrentDate,
    exp.CurrentTimestamp,
    exp.DateDiff,
    exp.DateAdd,
    exp.Extract,
)


class SqlGuardError(ValueError):
    """Raised when a statement fails structural validation.

    Carries a machine-readable ``reason`` so rejections can be counted by
    category in metrics rather than only logged as text.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True, slots=True)
class GuardedQuery:
    """A statement that passed validation, rewritten with a row limit."""

    sql: str
    tables: frozenset[str]
    row_limit: int


def _table_name(table: exp.Table) -> str:
    """The bare relation name, ignoring database and schema qualifiers.

    `AxonERP.dbo.vw_ai_shipments` and `vw_ai_shipments` name the same thing,
    and the check must not be defeated by qualifying a forbidden table.
    """
    return (table.name or "").lower()


def validate_read_only_sql(sql: str, *, max_rows: int = 500) -> GuardedQuery:
    """Parse, validate and bound a generated statement.

    Args:
        sql: The statement to validate.
        max_rows: Row cap injected when the query has no limit of its own.

    Returns:
        The validated statement with a row limit applied.

    Raises:
        SqlGuardError: On anything that is not a single, bounded SELECT over
            the approved views.
    """
    if not sql or not sql.strip():
        raise SqlGuardError("empty_statement", "no SQL provided")

    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:  # sqlglot raises several parse error types
        # We cannot validate what we cannot parse, so we refuse it.
        raise SqlGuardError("parse_failed", str(exc)) from exc

    statements = [s for s in statements if s is not None]

    if not statements:
        raise SqlGuardError("empty_statement", "statement parsed to nothing")

    if len(statements) > 1:
        raise SqlGuardError(
            "multiple_statements",
            f"{len(statements)} statements found; stacked queries are not permitted",
        )

    tree = statements[0]

    if not isinstance(tree, exp.Select | exp.Subquery):
        raise SqlGuardError(
            "not_a_select",
            f"top-level node is {type(tree).__name__}, only SELECT is permitted",
        )

    # Walk every node. Anything not explicitly allowed is a rejection.
    for node in tree.walk():
        if not isinstance(node, _ALLOWED_NODES):
            raise SqlGuardError(
                "forbidden_node",
                f"expression type {type(node).__name__} is not permitted",
            )

    tables = frozenset(_table_name(t) for t in tree.find_all(exp.Table))

    # A CTE name looks like a table reference; it is not one.
    cte_names = {(cte.alias or "").lower() for cte in tree.find_all(exp.CTE)}
    referenced = {name for name in tables if name and name not in cte_names}

    if not referenced:
        raise SqlGuardError("no_table_referenced", "query reads no approved view")

    forbidden = referenced - ALLOWED_VIEWS
    if forbidden:
        raise SqlGuardError(
            "forbidden_table",
            f"query references {sorted(forbidden)}; "
            f"only the approved views may be read: {sorted(ALLOWED_VIEWS)}",
        )

    # Bound the result. An unbounded query cannot blow out the context window
    # or the bill even if it is otherwise legitimate.
    existing = tree.args.get("limit")
    if existing is None:
        tree = tree.limit(max_rows)
        effective_limit = max_rows
    else:
        try:
            effective_limit = min(max_rows, int(existing.expression.name))
        except (AttributeError, ValueError):
            tree = tree.limit(max_rows)
            effective_limit = max_rows
        else:
            if effective_limit != int(existing.expression.name):
                tree = tree.limit(effective_limit)

    return GuardedQuery(
        sql=tree.sql(dialect=DIALECT),
        tables=frozenset(referenced),
        row_limit=effective_limit,
    )
