# Verification validity acceptance results

October 20, 2026, local branch `improve/verification-evidence-validity`, baseline
`ad90a2fc616f489ff17a748bea4f7f6ef2c617b4`. Windows, CPython 3.12.13, locked `uv sync --frozen` dependencies.

| Check | Actual result |
|---|---|
| Original versus repaired full grading module, identical controlled inputs | 3 previously confirmed invalid/ambiguous cases now inconclusive; valid recovery, continued warming and no-data controls unchanged |
| Dedicated new unit/service regression cases | 28 passed as part of suites below |
| Focused verification suite, after generating recordings | 53 passed, zero skipped |
| Complete unit suite | 717 passed, zero skipped; one dependency deprecation warning |
| Governance against disposable Postgres 16 + pgvector | 23 passed, zero skipped; includes two new persistence regressions |
| Ruff + format | Passed; 215 files formatted at final check |
| Strict mypy | Success, 133 source files |
| Import boundaries | 11 kept, zero broken |
| `git diff --check` | Passed |

Generated 60 deterministic scenario runs with `python -m simulator.incidentforge.cli run-all`.
Set `AXON_REQUIRE_INTEGRATION=1` for the complete unit suite and governance suite,
so missing recordings or database cannot produce a green skip.

Database verification used only a newly created local container on loopback port
55439. Alembic applied migrations 0001 and 0002 to that disposable database. The
tests executed approval, action, verification, committed state, refreshed the
verification and incident from Postgres, and checked the completion audit payload
and hash chain. The normal valid-recovery and failed-recovery controls also passed.

The new unit service tests mock persistence collaborators; they are not the basis
for the database integration claim. All temperature evidence in the two database
regressions uses the normal Evidence constructor. Normal ingestion rejects NaN
already; the core finite-value checks provide defense in depth.

No live LLM/provider call, paid service, new cloud infrastructure, push, PR or
deployment was performed. No frontend changed, so there is no new browser UI
acceptance claim. Full SQL Server integration/security, full application rehearsal,
production operation and GPU experiments were not run. Historical C11 results are
unchanged; this is not a new held-out sensor-health benchmark.

Acceptance closed for this bounded validity repair. Additional sensor-health,
temporal-coverage and real-world integration work is explicitly outside it. Do not
start another Axon feature merely to increase technology coverage.
