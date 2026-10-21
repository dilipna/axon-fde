# Verification release: evidence and limits

Candidate release, October 21, 2026. Local release qualification has passed;
remote CI and deployment approval are pending.
Protocol committed before implementation at `1129ec7`; baseline `af2741f`.
The public [machine-readable evidence](../apps/control_tower/verification-release.json)
contains both complete case reports, the input hash and historical regression.

## The defect and decision

Unique, finite readings can still be stale, emitted in a tiny burst, or stuck.
The service also allowed early finalization despite the scheduler listing only
closed windows. Calling the service directly could consume its one immutable
verdict before the observation period finished.

The service now rejects an early request without finalizing a verdict. The pure
grader requires an observation within two minutes of the deadline and a
two-minute recovery hold. An exactly constant otherwise-confirming trace abstains.
These are explicit one-minute-simulator policies, not field-calibrated SLAs.
The service persists inconclusive outcomes with follow-up and keeps incidents
unresolved. No LLM or new infrastructure is needed to make these decisions.

## Frozen 24-case comparison

| Metric | Baseline | Candidate |
|---|---:|---:|
| False confirmations / 12 true failures | 6 (50%) | 2 (16.7%) |
| Missed recoveries / 12 true successes | 0 | 4 (33.3%) |
| Abstentions / 24 cases | 4 (16.7%) | 12 (50%) |
| Declared expected verdicts matched | 16/24 | 24/24 |

All target-fault cases avoid confirmation; all fresh nonconstant honest controls
retain the correct verdict. Two deliberately nonconstant lying traces still
falsely confirm. The honest constant control is observationally identical to a
stuck sensor hiding failure: abstention necessarily costs a missed recovery.
This small constructed pack demonstrates specific behavior, not population
accuracy, statistical significance or production logistics safety.

## Historical scenarios, separately

The original C11 claim remains **0.94 agreement, 3 false confirmations**.
On its existing 100 trajectories, using truth labels frozen from the baseline
before this repair, the candidate has 0 false confirmations, 6 missed recoveries
and 6 abstentions; baseline has 3 false confirmations, 3 missed recoveries and
0 abstentions. Twenty windows remain excluded. Recomputed truth labels match
all 100 frozen labels. This is a regression diagnostic on familiar data, not an
independent validation set or a replacement for the historical published run.
The increased missed-recovery count must accompany any mention of fewer false
confirmations. Independent corroboration remains absent.

## Reproduction

Use Python 3.12 and the locked dependencies (`uv sync --frozen`).

```powershell
uv run python -m scripts.verification_release --output verification.json
uv run pytest tests/unit/test_verification_release.py tests/unit/test_verification.py tests/unit/test_verification_validity.py tests/unit/test_verification_claim.py -q
uv run pytest tests/integration/test_governance.py -q
uv run poe check
```

Database tests truncate the configured test database. Use fresh disposable
Postgres and SQL Server instances, migrate and seed them; never reuse a working
application database. This release uses loopback ports 55439 and 51439.
`AXON_LLM_MODE=cassette`, `AXON_ENV=ci` and `AXON_REQUIRE_INTEGRATION=1` prevent
live provider calls and silent dependency skips.

Baseline focused suite: 53 passed. New fault-pack and early-finalization tests
both failed before implementation. Fresh local qualification:

- Full unit run: 719 passed; 43 affected checks passed after the final provenance,
  snapshot and input-hash updates. The latter run includes the added snapshot test.
- Real Postgres and SQL Server integration/security: 247 passed, zero skips.
  Includes persisted ambiguity, early rejection, audit, approval, concurrency,
  legacy permissions and execution-gate tests. Databases were disposable.
- Cassette workflow and end-to-end demonstration: 44 passed, zero skips.
- Ruff/format clean; strict mypy passes across 133 source files; 11 architecture
  contracts kept. No dependency bypass or reduced application test assertions.
- Chrome at 1440, 768 and 390 pixels: replay/restart controls, one H1, no horizontal
  overflow or runtime exceptions; the JSON download matches the frozen pack.

Actual runtime: Python 3.12.13, pytest 9.1.1 on Windows. Tests explicitly loaded
asyncio/respx/Hypothesis/anyio/Faker plugins, with automatic plugin discovery
disabled after initial sandboxed runs stalled. A combined run was interrupted
and replaced by the complete separated suites above; it is not reported as a
pass. These tests used the unrestricted local process permission because the
sandbox blocked sockets/process activity. One unrelated Starlette/AnyIO
deprecation warning occurred in the unit run. No model provider was contacted.

## Technical ownership

Explain the difference between evidence validity, temporal sufficiency and
sensor truth. Explain why a flat healthy sensor must appear in the fault pack,
why conservative abstention has a cost, and why the service must enforce the
same deadline as its scheduler. No business savings or real customer deployment
is claimed; this is a personal system over simulated logistics data.

The proposed PII-vault integration from idea 14 is deferred: this demonstration
uses synthetic entities and no real personal-data requirement has been established.
Existing typed tools, approval binding, workflow checkpoints and budgets are
retained. Idea 15 is applied through this reproducible defect/design/results
case study, rather than adding another framework.
