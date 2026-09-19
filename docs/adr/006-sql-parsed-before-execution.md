# ADR-006 — Generated SQL is parsed and allowlisted before execution

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE

## Context

The dispatcher sidebar (`/query/nl`) lets someone ask "which pharma loads are
running more than 30 minutes late today?" and get an answer from the ERP. That
requires generating SQL from natural language.

ADR-005 already removes the privilege to do damage: the `axon_ai_ro` login can
only `SELECT` from six views. So why validate the SQL at all?

Three reasons.

1. **Defence in depth.** The grant is configuration. Configuration drifts,
   gets restored from a wrong backup, or is set up differently in a new
   environment. A control that depends on exactly one correctly-applied grant
   is one mistake away from nothing.
2. **The grant does not cover everything.** A syntactically valid `SELECT` can
   still be a cross-join that runs for minutes, a recursive CTE, or a stacked
   statement that the driver may or may not reject.
3. **Observability.** A rejected query with a structural reason
   (`node type Insert not permitted`) is a security signal we can count.
   A permission error from the database is a signal we get much later, at a
   layer with less context, and it is harder to attribute to a specific attack.

The wrong way to do this is string matching. `"DROP" in sql.upper()` fails on
`SELECT * FROM vw_ai_shipments WHERE customer_name = 'DROP Logistics'` and
misses `SEL/**/ECT`. Regex-based SQL guards are security theatre.

## Decision

**Every generated statement is parsed into an abstract syntax tree with
SQLGlot (`tsql` dialect) and validated structurally before it reaches the
driver. Validation is an allowlist, not a denylist.**

### The validation pipeline

```
generated SQL
   ↓  parse with sqlglot.parse(dialect="tsql")
   ↓  reject if parsing fails            → cannot validate what we cannot parse
   ↓  reject if more than one statement  → no stacked queries
   ↓  walk every AST node
   ↓  reject if any node type is not on the allowlist
   ↓  reject if any referenced table is not one of the six views
   ↓  inject TOP n if no row limit is present
   ↓  execute with a statement timeout, as axon_ai_ro
```

### Allowlist, not denylist

Permitted node types: `Select`, `From`, `Where`, `Join`, `Group`, `Having`,
`Order`, `Limit`, `Subquery`, `CTE` (non-recursive), plus expressions,
literals, identifiers and a fixed set of scalar and aggregate functions.

Everything else is rejected, including anything added in a future SQLGlot
version. A denylist would need updating every time the grammar grows; an
allowlist fails safe by default.

### Table-level checks

The AST is walked for table references and each is checked against the six
`vw_ai_*` views. A query naming `dbo.shipments` is rejected before execution
even though the grant would also have rejected it — and the rejection is
attributable, logged, and counted.

## Alternatives considered

### A. Rely on the read-only grant alone

Rejected on defence in depth. It is one misconfiguration away from no control
at all, and it produces poor security telemetry.

### B. Keyword / regex denylist

Rejected. Both over-blocks legitimate data and under-blocks real attacks.
Comment injection, unicode homoglyphs, and string literals containing keywords
all defeat it. This is the approach the ADR exists to rule out.

### C. Never generate SQL — only typed queries

This is exactly what the **incident workflow** does, and it is the right call
there (see ADR-003): safety-critical investigation uses typed repository
methods, never generated SQL.

Rejected for the sidebar, because ad-hoc questions were the customer's original
request and typed endpoints cannot anticipate them. Keeping generated SQL
confined to the sidebar means the blast radius is one bounded feature, and it
also gives claim C8 a clean, isolated test surface.

### D. A dedicated SQL-validation service

Rejected: a network hop and a deployment unit for a pure function. See ADR-002.

## Consequences

### Accepted costs

- **Some legitimate queries are rejected.** A question needing a window
  function or a recursive CTE fails until the construct is deliberately added
  to the allowlist. This is the correct direction to fail, and the rejection
  message names the offending node type so the gap is visible.
- **SQLGlot's `tsql` parser is a dependency** whose behaviour matters. Pinned
  to a major version; the ~60-case adversarial suite runs on every PR and would
  catch a behavioural change on upgrade.
- **Parsing cost** on every query. Negligible relative to the round trip.

### Gained

- Claim C8 becomes measurable: prohibited-operation rate across ~60 adversarial
  inputs, with a target of exactly zero and a hard CI gate.
- Rejections are attributable and countable, so an injection attempt through
  the sidebar is visible in metrics rather than buried in database errors.
- Three independent layers (AST, grant, timeout/row-cap) must all fail before
  anything harmful executes.

## Verification

The security suite covers, at minimum:

| Category | Examples |
|---|---|
| Direct DDL/DML | `DROP TABLE`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `ALTER` |
| Stacked statements | `SELECT 1; DROP TABLE shipments` |
| Comment evasion | `SEL/**/ECT`, `--` and `/* */` terminators |
| Unicode / encoding | homoglyphs, escaped identifiers |
| CTE-wrapped writes | `WITH x AS (...) INSERT INTO ...` |
| Procedure execution | `EXEC xp_cmdshell`, `sp_executesql` |
| Unauthorised tables | `SELECT * FROM dbo.shipments` (base table, not a view) |
| Resource exhaustion | unbounded cross joins, missing row limits |
| Benign controls | legitimate queries that **must not** be blocked |

The last row matters as much as the others. A validator that blocks everything
passes every attack test and is useless, so false-positive rate on benign
queries is reported alongside the attack results.
