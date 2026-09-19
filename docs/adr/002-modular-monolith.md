# ADR-002 — Modular monolith before services

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE

## Context

AxonFDE has a dozen distinct concerns: evidence normalisation, retrieval, risk
scoring, decision arithmetic, policy, approvals, action execution, verification,
audit, and the agent workflow that coordinates them. Drawn on a whiteboard,
they look like services.

The forces that normally justify splitting them do not apply here:

- **Independent scaling** — there is no component whose load profile differs
  enough to need separate capacity. The system handles tens of incidents an
  hour, not thousands a second.
- **Independent deployment** — one team, one release cadence.
- **Fault isolation** — the incident loop is a *transaction* of sorts. An
  investigation that loses its risk service mid-flight does not degrade
  gracefully into a useful partial answer; it degrades into an escalation.
- **Team ownership** — there are no separate teams to own separate services.

Meanwhile the forces against splitting are strong: distributed tracing across a
dozen services, network failure modes between every pair, transactional
integrity lost across the evidence/audit boundary, and a local development
environment that would not fit on a 15.7 GB machine.

## Decision

**One deployable FastAPI application with hard internal module boundaries**,
plus two separate long-running processes that genuinely differ in lifecycle:

| Process | Why separate |
|---|---|
| `api` | Request/response, serves the UI |
| `consumer` (Phase 2) | Consumes the telemetry stream continuously; must not be restarted by an API deploy mid-offset |
| `worker` | Scheduled verification and benchmark runs; long tasks that would block request handlers |

All three import the same modules. The split is about **lifecycle**, not
about domain boundaries.

Boundaries are enforced by `import-linter` contracts in `pyproject.toml`, so
modularity is a build failure rather than a code-review preference:

- The domain layer imports no other internal layer.
- Only `db.legacy` may import `pyodbc`.
- The policy engine is pure: no database, no HTTP, no cloud SDK.
- The decision engine is pure and additionally may not import the LLM module.
- The simulator does not depend on the application.

## Alternatives considered

### A. Microservices from the start

Rejected. It would demonstrate familiarity with a deployment pattern while
actively making the system worse: more failure modes, slower iteration, harder
evaluation, and a local environment that cannot run. Choosing it would be a
resume decision rather than an engineering one.

### B. A single package with no enforced boundaries

Simpler still, and what most projects of this size actually do.

Rejected because the boundaries here carry *security* meaning, not just tidiness.
"The policy engine cannot perform I/O" and "only one module may reach the legacy
database" are claims the project makes. An unenforced claim decays the first
time someone is in a hurry.

### C. Serverless functions per capability

Rejected. Cold starts on an interactive investigation path, no durable agent
checkpointing, and per-function packaging of a LightGBM artifact.

## Consequences

### Accepted costs

- **Scaling is all-or-nothing.** If one component ever becomes a bottleneck,
  the whole application scales with it. Acceptable at this load; revisit if
  measurement shows a genuine hotspot.
- **A module boundary is easier to erode than a network boundary.** Mitigated
  by the linter contracts, but the contracts have to be maintained as the
  system grows — each new module needs a deliberate decision about what it may
  import.

### Gained

- Transactional integrity across evidence, hypotheses and audit writes.
- One trace per incident with no cross-service correlation to reconstruct.
- A local environment that fits in memory alongside two databases.
- Extraction remains possible later: the enforced boundaries are exactly the
  seams a service would be cut along, so this is a deferral rather than a
  foreclosure.

## Revisit if

- A load test shows one component saturating well before the others.
- A second team takes ownership of a distinct capability.
- The telemetry consumer's throughput requirements diverge sharply from the
  API's — the most likely first candidate for genuine extraction.
