# Verification evidence validity

New portfolio repair, October 20, 2026. This does not rewrite the historical C11 benchmark.

## Problem and design

`evaluate_effect` previously confirmed a recovery for three NaNs because comparison with NaN is false in both directions. It also counted three copies of one timestamp as three recovery observations. A slope fit could overweight repeated delivery, and non-finite computed slopes passed the final comparison.

The normal `Evidence.create` path already rejects NaN outside its plausible numeric range. The new finite checks protect the grading boundary itself; the NaN counterexample is not a demonstrated deployed ingestion exploit. Repeated/conflicting finite readings are exercised through real evidence objects and the governance service.

Before grading measurable effects:

1. Require an ordered, timezone-aware window and finite ordered envelope bounds.
2. Consider values only inside that window; an unlocatable naive timestamp abstains.
3. Abstain for in-window non-finite values or boolean pseudo-temperatures. Do not silently hide invalid evidence by dropping it.
4. Collapse exact timestamp/value repeats. Conflicting values at one timestamp abstain because this interface has no sensor identity or reconciliation rule.
5. Apply the existing recovery hold, slope sample/coverage and temperature thresholds to unique observations. Require a finite computed slope.

The result remains INCONCLUSIVE with follow-up required when evidence cannot settle it. The existing service stores the verdict/audit record and leaves the incident VERIFYING. NONE_OBSERVABLE remains NOT_APPLICABLE. No model, new dependency, schema migration or deployment is introduced.

## Alternatives and costs

Silently discarding invalid/conflicting values can manufacture an apparently clean trace. Choosing one conflicting value makes arrival order consequential. Changing the global Evidence model would affect unrelated adapters. Inferring all sensor failures requires separate health/provenance data and an evaluation pack.

The new normalization requires O(n) memory for unique timestamps and O(n log n) sorting, the same asymptotic time as the prior sort. No runtime latency improvement is claimed. Conservative abstention can increase human review. A finite stuck sensor can still falsely confirm success; this repair does not establish sensor reliability, minimum temporal spacing, freshness at the window end or independence across sensors.

## Reproduce

```powershell
uv sync --frozen
uv run python -m simulator.incidentforge.cli run-all
uv run pytest tests/unit/test_verification_validity.py tests/unit/test_verification.py tests/unit/test_verification_claim.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy backend/app simulator benchmarks
uv run lint-imports
```

For actual database verification use a **disposable** migrated Postgres instance. Integration fixtures truncate their configured database. Do not point them at an existing application database. Set POSTGRES_PORT to the disposable port, run the project's Alembic migrations, set AXON_REQUIRE_INTEGRATION=1, then run `pytest tests/integration/test_governance.py -q`. No SQL Server or live LLM is required for this governance suite. Tests cover successful and failed intervention controls alongside duplicate/conflicting evidence, persisted verdicts, unresolved incident state and audit-chain integrity.

See [acceptance results](verification-validity-results.md) for actual counts and exclusions,
and the mission completion report for the local commit. A mocked service test is
explicitly labeled; it does not replace the Postgres tests.

## Public-safe case study and three-minute walkthrough

Problem: a simulated cold-chain integration must verify the action outcome rather than resolve an incident merely because an action ran. Constraint: the verifier receives timestamp/temperature pairs without source identity. Design: normalize duplicate delivery and decline conflicting or invalid evidence before applying existing domain thresholds. Evidence: run the focused tests and inspect the real database governance regression. Tradeoff: more explicit abstention without pretending to detect lying finite sensors.

Demo: first show the old three-repeated-readings counterexample; run the new focused suite; show three distinct valid readings still confirming; inspect the governance regression's persisted INCONCLUSIVE/VERIFYING result and audit payload. Explain why the historical 94% C11 figure is not a new result and why production logistics performance is not claimed.

## Interview preparation

- What failed? Comparisons can treat NaN as in-range; row counts can confuse repeated delivery with independent observation.
- Why not just add an LLM? This is deterministic evidence validation with exact counterexamples.
- Why abstain on simultaneous disagreement? This layer lacks the source identity needed to reconcile conflicting sensors.
- What changes internally? Normalize unique times before both graders; preserve existing thresholds and lifecycle decisions.
- What is the failure path? INCONCLUSIVE, human follow-up, incident remains VERIFYING with audit evidence.
- What did testing establish? Narrow invalid/duplicate/conflict cases and valid controls, with database persistence tested separately.
- What did it not establish? Sensor trust, production deployments, general safety or a new C11 benchmark improvement.
- How would you extend it? Add identified sensors, independently justified health/corroboration rules and a new preregistered fault pack reporting false confirmations, missed recoveries and abstentions together.

Candidate project bullets, conditional on the completion checks:

- Hardened a simulated enterprise intervention verifier to abstain on invalid or conflicting evidence and deduplicate repeated observations before recovery/trend decisions.
- Validated approval-to-outcome handling against disposable Postgres, checking that ambiguous readings preserve an unresolved incident, persist follow-up and retain a valid audit chain.
