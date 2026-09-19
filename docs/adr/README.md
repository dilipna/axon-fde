# Architecture Decision Records

Each record captures one decision, the forces acting on it, the alternatives
considered, and the consequences accepted. Records are immutable once accepted:
a reversal is a new record that supersedes the old one, so the reasoning at the
time is preserved rather than rewritten.

**Format:** `NNN-short-slug.md`
**States:** `Proposed` · `Accepted` · `Superseded by NNN` · `Reversed by NNN`

## Index

| ADR | Title | State | Phase |
|---|---|---|---|
| [001](001-proactive-incident-response.md) | Solve proactive incident response, not conversational retrieval | Accepted | 0 |
| [002](002-modular-monolith.md) | Modular monolith before services | Accepted | 0 |
| [003](003-one-typed-workflow.md) | One typed workflow, not a multi-agent swarm | Accepted | 0 |
| [004](004-risk-outside-the-llm.md) | Operational risk is computed outside the LLM | Accepted | 0 |
| [005](005-read-only-legacy-access.md) | AI access to the legacy system is read-only, through views | Accepted | 0 |
| [006](006-sql-parsed-before-execution.md) | Generated SQL is parsed and allowlisted before execution | Accepted | 0 |
| [007](007-typed-normalized-evidence.md) | All evidence normalises to one typed, provenance-carrying model | Accepted | 0 |
| 008 | Approvals are bound to the evidence they were granted against | Planned | 1 |
| 009 | Streaming is justified by replay, not by throughput | Planned | 2 |
| 010 | PostgreSQL + pgvector is sufficient; no second vector store | Planned | 1 |
| 011 | ECS Fargate rather than Kubernetes | Planned | 7 |
| 012 | Evaluation ships with the first slice, not after it | Planned | 1 |
| 013 | Intervention outcomes are verified by a separate scheduled workflow | Planned | 1 |
| 014 | No confidence score originates from a language model | Planned | 1 |
| 015 | Structured extraction and citations are separate API calls | Planned | 4 |
| 016 | Async application database, synchronous legacy driver | Planned | 1 |

## Why these particular decisions get records

A decision earns an ADR when it is **expensive to reverse**, **contested**, or
**counterintuitive without its context**. Decisions that are merely
conventional (using Pydantic for schemas, pytest for tests) do not get records
— an index full of obvious choices makes the load-bearing ones harder to find.
