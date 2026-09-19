# ADR-005 — AI access to the legacy system is read-only, through views

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE, Axon IT

## Context

Axon's ERP on SQL Server is the system of record for shipments, customers,
vehicles, routes and maintenance. It is the source of most of the evidence
AxonFDE reasons over.

Axon IT stated their constraint plainly during discovery: **no new system gets
write access to the ERP.** They have three staff, a long backlog, and no
appetite for debugging data corruption caused by a system they did not build.

That constraint is correct on its own merits, and it is sharper than it first
appears. It is not only about writes. An LLM-driven system reading an ERP can
also:

- pull columns nobody intended it to see (pricing terms, driver personal data),
- run an expensive query that degrades the ERP for the humans who depend on it,
- return an unbounded result set that blows out the context window and the bill.

A system prompt saying "only issue SELECT statements" addresses none of this.
It is a request, not a control, and it is the first thing an injected
instruction will try to override.

## Decision

**The application reaches SQL Server through a dedicated login that holds
`SELECT` on six purpose-built views and nothing else.**

### The grant

```sql
CREATE LOGIN axon_ai_ro WITH PASSWORD = ...;
CREATE USER  axon_ai_ro FOR LOGIN axon_ai_ro;

-- Deliberately NOT db_datareader: that would grant every base table.
GRANT SELECT ON dbo.vw_ai_shipments            TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_vehicles             TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_cargo_requirements   TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_maintenance          TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_facilities           TO axon_ai_ro;
GRANT SELECT ON dbo.vw_ai_historical_incidents TO axon_ai_ro;
```

### The views

Each view is a **projection**, never `SELECT *`. Columns the AI has no business
seeing never leave the database, so no downstream mistake can expose them. When
a new column appears on a base table, it is invisible to the AI until someone
deliberately adds it to a view — the safe default.

### Additional layers

| Layer | Purpose |
|---|---|
| Statement timeout (5s) | A pathological query cannot degrade the ERP |
| Row cap (`TOP n`, default 500) | An unbounded result cannot blow the context or the bill |
| AST allowlist (ADR-006) | Structural validation before anything reaches the driver |
| `LegacyRepo` module boundary | Only `db.legacy` may import `pyodbc`, enforced by the linter |

### We never migrate this schema

The legacy database is seeded with raw T-SQL and is never touched by Alembic.
Alembic manages our PostgreSQL only. This asymmetry is deliberate and is the
integration story: we treat the ERP as someone else's system, because it is.

## Alternatives considered

### A. `db_datareader` on the whole database

Simpler, and what most integrations do.

Rejected. It grants every base table including ones that do not exist yet, so
the blast radius silently grows with the customer's schema. It also makes the
"the AI cannot see pricing data" claim untestable.

### B. Read replica with full access

Protects the production ERP from load but not from data exposure, and adds
infrastructure Axon does not have. The load concern is already handled by the
timeout and row cap.

### C. Nightly ETL into our PostgreSQL, query only our copy

Genuinely attractive: total isolation, no load on the ERP, one dialect.

Rejected because incident response needs current data. A shipment rerouted an
hour ago must be visible now, and an ETL lag would produce exactly the
stale-evidence failure mode the system is designed to detect. A batch adapter
for genuinely static reference data remains a reasonable later addition.

### D. Trust the LLM to only emit SELECT

Rejected outright. Not a control. This is the single most important thing this
ADR exists to rule out.

## Consequences

### Accepted costs

- **Adding a field the AI needs requires a view change**, which is a deliberate,
  reviewable act with a migration script. Slower than "just query it," and that
  friction is the feature.
- **Six views to maintain** alongside the base schema.
- **Some joins must happen in the application** rather than in the database,
  because the views are deliberately narrow. Acceptable at this data volume.

### Gained

- "The AI cannot write to the ERP" becomes a testable claim (C8) rather than an
  assurance. An integration test asserts that `INSERT`, `UPDATE`, `DELETE` and
  a base-table `SELECT` all fail for `axon_ai_ro`.
- The exposure surface is enumerable: six views, explicit columns.
- Prompt injection cannot escalate, because there is no privilege to escalate to.

## Verification

Integration tests run against a real SQL Server container and assert:

- [ ] `SELECT` succeeds on each of the six views
- [ ] `SELECT * FROM dbo.shipments` (base table) **fails** with a permission error
- [ ] `INSERT`, `UPDATE`, `DELETE`, `DROP` each **fail**
- [ ] `EXEC xp_cmdshell` **fails**
- [ ] A query exceeding the statement timeout is cancelled
- [ ] No query returns more than the configured row cap
