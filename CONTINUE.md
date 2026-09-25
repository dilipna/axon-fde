# CONTINUE — session handoff

Read this first in any new session. It is the single source of truth for where
AxonFDE stands and what to do next.

**Keep it current.** The last action of every session is to update this file:
mark the block done, record anything learned, and set the next block.

---

## 0. A note on pace

The phases in `~/.claude/plans/` are 3–4 weeks of full-time work each. One is
not completable in a session, and pretending otherwise produces half-finished
phases that are worse than none.

The remaining work is therefore broken into **blocks** (§6). A block is sized
to finish, verify, and commit inside one session. Two blocks in a good session
is realistic. Each block leaves the repository green and releasable.

---

## 0.1 The fastest path from here

Read this before choosing what to do. It is the difference between finishing
Phase 1 and half-finishing three blocks.

**One decision gates everything, and only you can make it.**

> **Do you have an `ANTHROPIC_API_KEY` you are willing to spend ~$2–5 on?**
>
> - **Yes** → record cassettes early in the session (B10b below). One recording
>   session unblocks the `rules+llm` arm permanently, because every later run
>   replays offline and free.
> - **No** → say so at the start. **Most of Phase 1 finishes without it**, and
>   the blocks below are ordered so the model-dependent work is last rather
>   than first. Do not let a missing key stall a session.

**Four claims are measured. Eleven are not, and the reasons differ.**

An earlier version of this section claimed seven were reachable without a
model. That was wrong, and B10a corrected it by reading each claim's stated
*method* rather than its metric. The methods carry dataset requirements:

| Claim | State | What it needs |
|---|---|---|
| **C7** unauthorised actions | ✅ `MEASURED` **0** | nothing — 50/50 cells |
| **C8** prohibited SQL | ✅ `MEASURED` **0** | nothing — 57/55 inputs |
| **C1** lead time | ✅ `MEASURED` **49 min @ FAR 0.30** | nothing — 40/40 breach scenarios, 20 controls |
| **C6** contradictions | ✅ `MEASURED` **recall 1.00, precision 1.00** | nothing — 23/20 seeded conflicts |
| C10 grounding | `INSUFFICIENT_DATA` | generated narratives → needs the LLM arm |
| C11 verification | `INSUFFICIENT_DATA` | post-action trajectories → **needs simulator work**, not the LLM |
| C13 degradation | `INSUFFICIENT_DATA` | the failure-injection suite → B14 |
| C2, C3, C4, C5, C9, C12, C14, C15 | `PLACEHOLDER` | later blocks |

**The lesson, and it is the useful part:** a claim's *metric* looks reachable
long before its *method* is. Read the method row before promising a number.
`poe bench` records the shortfall for every blocked claim, so this does not
have to be rediscovered — run it and read `docs/evaluation/results.md`.

**B10c is the proof of that lesson twice over.** Its own brief, written here,
said "five more breach scenarios" would unblock C1 — while the table three
lines above said C1 needs **forty**. Both sentences were in this file at once.
Five would have left C1 at `INSUFFICIENT_DATA` with the work already spent. The
brief also said `poe bench` "grades whatever the pack contains"; it did not —
no C1 or C6 grader existed, only the two safety ones, and every other claim got
a stub. **Re-derive a block's size from the register, not from the last
session's summary of it.**

**Never quote C1's 49 minutes on its own.** The false-alarm rate is 0.30
against the baseline threshold alarm's 0.20, and `claims.md` calls the
unpaired number a misuse. There is also a second median in the stored run —
**35 min** over the alerts that followed their fault — because on 22.5% of
breach scenarios the detector fired *before the fault started*. See the C1
finding in §8.

**The critical path to the portfolio checkpoint (end of B13):**

```
B10a (no key)  ->  B10b (one recording)  ->  B13 multimodal
                                                                           ->  B12 trained model [has a stop condition]
B11 streaming  [has a stop condition - cut it if the criteria are not met]
```

B11 and B12 both carry explicit **stop conditions** (§6). Honour them. Cutting
a block that fails its criteria and writing the ADR is *finishing* it, not
skipping it, and it is worth more than a half-built Redpanda integration.

**What actually makes a session slow**, measured across the last four:

1. Docker is down, or drops mid-session. Start it first and re-check after any
   long gap - it died twice in one session.
2. Guessing at a name instead of reading it. Three separate bugs came from
   invented observation types, an invented `ShipmentRecord` field and an
   invented `ChainVerification` field. Grep before writing.
3. Running plain `poe check` and discovering CI disagrees. Always run the real
   gate (§4) before pushing.
4. **The gate is now slow** — the unit suite alone is about two minutes, most
   of it `tests/unit/test_axonbench.py` sweeping two detectors over sixty
   scenarios. Start the full gate in the background and do something else;
   do not pipe it through `tail`, which withholds all output until it ends.
   `run_arm("rules_only")` is a module-scoped fixture for the same reason — it
   was being recomputed eight times at sixteen seconds each.

## 1. Invariants — never violate these

These are the architecture. Breaking one silently is worse than not shipping
the feature. Each is enforced by a test, a linter contract, or a database
constraint, and each has an ADR.

| # | Invariant | Enforced by |
|---|---|---|
| I1 | **A language model can never be the source of an observation.** It links, interprets and narrates evidence; it never creates it. | No LLM member in `EvidenceSource`; DB CHECK `ck_evidence_source_never_llm`, reached by raw SQL in `test_an_llm_authored_observation_is_rejected_by_the_database`; `test_llm_is_not_a_valid_evidence_source` |
| I2 | **Confidence is computed, never supplied.** Single implementation in `Taxonomy.confidence()`. No constructor accepts a caller-provided confidence. | `Evidence.create()` is the only construction path |
| I3 | **Risk probability is computed outside the LLM**, and always stored beside its non-ML baseline. | `RiskAssessment.baseline_probability` is `NOT NULL`; import-linter forbids `decision` importing `anthropic` |
| I4 | **The AI cannot write to the legacy ERP.** Read-only login, six views, AST allowlist, timeouts. | `tests/integration/test_legacy_access.py`, `tests/security/test_sql_guard.py` |
| I5 | **No consequential action executes without a valid, unexpired, hash-matching approval.** | `ActionExecutor.execute` asks `may_execute_immediately`, never `allowed`; `tests/security/test_execution_gate.py` enumerates every gated action and asserts the `action_execution` table stays empty |
| I6 | **Never fabricate a missing observation.** Absence is represented as absence and lowers confidence. | `resolve_envelope()` returns `None` rather than a default, proven by `test_an_unknown_shipment_yields_no_envelope_rather_than_a_default`; empty fault code lists are stored, not dropped; degradation ladder in `docs/architecture/overview.md` §7 |
| I7 | **No number ships without a stored benchmark run behind it.** | `docs/evaluation/claims.md` |
| I8 | **Ground truth never reaches the application.** `TelemetryEvent` carries only what a sensor could report; `GroundTruthFrame` is benchmark-only. | `test_telemetry_events_carry_no_ground_truth`, `test_written_telemetry_contains_no_ground_truth`, and `test_the_telemetry_endpoint_serves_no_ground_truth` for the API the UI reads |
| I9 | **Policy and decision modules are pure** — no I/O, no LLM. | import-linter contracts in `pyproject.toml` |
| I10 | **Audit history is append-only.** It cannot be edited through the application's credentials, nor by a connection that owns the table. | Grants on `axon_app` (SELECT + INSERT only), `trg_audit_event_append_only`, HMAC chain; `tests/integration/test_app_db.py` asserts the row is *unchanged* afterwards, not merely that an error was raised |

### Working style that produced the good findings so far

When a test fails, **decide whether the test or the code is wrong before
fixing either.** Three real bugs were caught this way, and in two cases the
*test* was wrong and fixing it revealed something worth documenting:

- A security test asserted `pyodbc.Error` on privilege escalation. The driver
  doesn't raise. The escalation had no effect, but the test was checking the
  mechanism rather than the outcome. → Security tests now assert **what an
  attacker ends up able to do**.
- An audit test asserted a rewritten hash chain stays detectable. It doesn't.
  → Added HMAC keying, and kept a test that **documents the unkeyed
  limitation** rather than hiding it.
- The plan said concurrent audit appends would *fork* the chain. Measured with
  the lock removed: they don't. `unique(seq)` refuses the second writer, so 12
  simultaneous appends store **one** event and raise eleven
  `UniqueViolationError`s. → The real failure is **losing audit events**, which
  for this table is worse than a fork: a fork is visible, a rejected append
  means a consequential action with nothing in the record and a chain that
  still verifies.
- A test hung instead of failing: the barrier needed all 12 writers holding a
  connection, and the pool capped at 10. → A test-only deadlock that reads as a
  database fault. The fixture is now explicitly sized and says why.

**Prove the test would fail.** Where a test protects something load-bearing,
break the code and watch it go red before trusting it. The concurrency test was
verified this way, and the first attempt failed with a SQLAlchemy cleanup error
that hid the real collision — so the diagnosis itself needed fixing.

**A test can pass against the exact bug it was written to catch.** B10c wrote
a subprocess test to prove C6's negative set does not depend on
`PYTHONHASHSEED`, then put the `hash()` bug back to check it — and it stayed
green. The test compared conflict counts, which are identical whether the
channel rotation is stable or reseeded every run; the assignment it was
actually about was not in the comparison. **Assert on the quantity the bug
moves, not on a downstream number that happens to be nearby.** The only reason
this was caught is that breaking the code deliberately is a habit here.

**A test that skips is a test that did not run.** `AXON_REQUIRE_INTEGRATION=1`
turns a missing dependency from a skip into a failure. CI sets it; developers
do not. See `tests/conftest.py`.

**A minimum count is not a window.** `MIN_READINGS_FOR_SLOPE = 5` looked like
an adequate guard and silently turned a "30-minute trend" into "any five
readings", fitting four minutes of pull-down transient. Two constants that
looked independent were coupled. Whenever a parameter names a duration, check
that something enforces the duration.

**Measure the thing you will ship, not a sketch of it.** The B3 prototype
evaluated only from `i = window` onward, so it never looked at the minutes
just before a full window existed — and missed that the real implementation
fires at minute 29. The corrected answer needed a three-reading hold.

**A cost parameter that flatters an action is a bug.** Two were caught by
tests: an inspection modelled as removing 15% of risk for 90 dollars made
"inspect everything" optimal at 2% risk, and a trailer swap modelled as more
effective than a reroute beat the scenario's declared correct action. Both
times the arithmetic was right and the input was wrong. When a decision
disagrees with ground truth, suspect the parameters before the engine — and
say plainly when a number was revised after seeing the disagreement.

**A gate nobody looks at is not a gate.** CI had failed on **all eight runs
since the first commit**, and it went unnoticed because `poe check` is green
locally. Five unrelated causes, all pre-existing:

1. `Settings(_env_file=None)` stops pydantic reading a `.env` file but **not**
   `os.environ`, so a test asserting "the default environment is local" passed
   on every laptop and failed on every CI run, where `AXON_ENV=ci` is exported.
2. gitleaks had no config and flagged the deliberately published dev
   credentials.
3. The ODBC install was pinned to Ubuntu 22.04 on a 24.04 runner.
4. `tests/agent/` is empty and pytest exits 5 when it collects nothing.
5. `gitleaks-action@v2` runs the scanner in a container and resolves
   `GITLEAKS_CONFIG` against a path that is not where the workspace is
   mounted, so the allowlist added to fix (2) was **silently ignored**. Now
   the pinned binary is invoked directly, so the CI command is exactly the
   command you can run locally.

**A disabled CI job is a step that has never run.** Three jobs have now failed
on their *first real execution* — the security job had no database, the agent
job reported an empty suite, and the AxonBench job carried a `--` inherited
from the placeholder step it replaced, which poethepoet forwards literally to
argparse. In each case the line looked fine and had never executed. When
enabling a job, run its exact command locally first, byte for byte.

**A new test suite may need a new service.** `poe check` green locally says
nothing about whether CI's *job for that suite* can run it. The security job
had no Postgres, so the twelve new execution-gate tests failed there while
passing everywhere else. `AXON_REQUIRE_INTEGRATION=1` is what made that a red
build instead of a silently shrinking test count.

**Check the badge after pushing.** `poe check` being green means nothing about
CI, which is the lesson all five of those share. The API works without auth:

```bash
curl -s "https://api.github.com/repos/dilipna/axon-fde/actions/runs?per_page=1" \
  | python -c "import json,sys; r=json.load(sys.stdin)['workflow_runs'][0]; \
    print(r['run_number'], r['status'], r['conclusion'])"
```

**A third-party action that swallows its own configuration is worse than no
action.** It failed in the direction of "your allowlist does nothing", which
looks identical to "your allowlist is wrong". Prefer invoking the tool.

Write tests that would fail if the claim were false. Prefer asserting outcomes
over mechanisms.

---

## 2. Current state

**39 commits · 842 tests · mypy --strict clean · 11 module contracts · **CI green on all six jobs** · pushed to
`https://github.com/dilipna/axon-fde`**

Repository: `C:\dev\axonfde` (deliberately **not** in OneDrive — sync corrupts
`.venv` and Docker mounts).

### Built and proven

| Component | Location | Notes |
|---|---|---|
| Observation taxonomy | `data/taxonomy/observations.yaml`, `backend/app/domain/taxonomy.py` | 18 types with tolerances, TTLs, plausible ranges, source reliability, authority order |
| Evidence model | `backend/app/domain/evidence.py` | Typed, provenance-carrying, computed confidence |
| Reconciliation | `backend/app/evidence/reconciliation.py` | Deterministic conflict / corroboration / supersession |
| Audit chain | `backend/app/audit/chain.py` | HMAC-capable, reports every break, no cascade |
| Legacy integration | `backend/app/db/legacy/`, `data/seed/legacy/*.sql` | 6 views, `axon_ai_ro` login, AST allowlist |
| IncidentForge | `simulator/incidentforge/` | Thermal ODE, ramped faults, seeded determinism, Parquet emitters, CLI |
| App schema models | `backend/app/db/app/models.py` | 12 tables |
| Migrations | `backend/app/db/migrations/` | `0001` schema + pgvector, `0002` append-only audit |
| Persistence | `backend/app/db/app/session.py`, `repositories/` | Async engine, evidence / incident / audit repositories |
| Audit service | `backend/app/audit/service.py` | Advisory-locked append; concurrent appends proven safe |
| Telemetry adapter | `backend/app/evidence/telemetry.py` | Parquet → Evidence, 12 observation types per reading |
| ERP adapter | `backend/app/db/legacy/repository.py` | Typed view reads → cargo-requirement and maintenance evidence |
| Document adapter | `backend/app/evidence/documents.py`, `data/documents/extractions/` | Seeded BOL extractions with page + bbox |
| Detection | `backend/app/incidents/detection.py` | Honest threshold baseline; `Detector` protocol both arms share |
| Incident lifecycle | `backend/app/incidents/lifecycle.py` | Transition table, dedup, 6-hour suppression window |
| Scenario replay | `backend/app/incidents/replay.py` | The pipeline end to end, `poe`-free and transaction-owned |
| Risk service | `backend/app/risk/` | Slope extrapolation + rule prior, one feature builder, lead time |
| Predictive detection | `backend/app/incidents/detection.py` | Fires at **minute 102**, 35 min before the threshold alarm |
| Policy engine | `backend/app/policies/engine.py` | Full 5x10 matrix, deny absolute, zero bypasses |
| Decision engine | `backend/app/decision/` | Costed candidates, EV ranking, feasibility, flip point |
| Auth | `backend/app/auth/tokens.py` | JWT to `Principal`; explicit algorithm allowlist, required `aud`/`iss`, unknown role refused not demoted |
| Approval binding | `backend/app/approvals/binding.py` | Pure; covers evidence hashes, the risk probability **and its baseline**, `degraded`, action, target, incident, binding version |
| Approval service | `backend/app/approvals/service.py` | Request / grant / deny / check; expiry and staleness are distinct codes and *all* refusals are reported |
| Action execution | `backend/app/actions/` | Ten deterministic simulators, expected-effect declarations, advisory-locked idempotency keyed on the approval |
| Outcome verification | `backend/app/verification/` | Deterministic grader; failed reopens the incident, inconclusive moves nothing |
| Closed loop demo | `scripts/demo.py`, `tests/e2e/test_demo.py` | `poe demo`, 13 steps, no model. Rolls back by default so it is repeatable; `--keep` commits. **The `rules_only` ablation arm for C5.** |
| LLM boundary | `backend/app/llm/` | `LLMProvider` protocol, Anthropic impl with prompt caching and structured outputs, cassettes that **fail loudly**, daily spend ceiling, per-call cost. Only `anthropic_provider` may import `anthropic` (contract). |
| Agent workflow | `backend/app/agents/` | 14 nodes, typed checkpointable state, budget gates *between* nodes, deterministic scorer owning every confidence, deterministic grounding check. Two model nodes, each followed by a check that can reject it. |
| Config / health / logging | `backend/app/config.py`, `api/v1/health.py`, `observability/logging.py` | Startup safety validation, TCP dependency probes, redaction |
| Control tower | `apps/control_tower/`, `backend/app/api/v1/control.py`, `api/ui.py` | `poe demo-trace && poe tower`. Static page, no build step. Three read-only endpoints. Renders the trace of a real run; says what is missing rather than showing fixtures |

### Scenario pack (`data/scenarios/pack_v1`, version **1.1.0**, 60 scenarios)

**40 breach · 20 control · 23 seeded cross-source conflicts.** The counts are
not round numbers chosen for tidiness: 40 is C1's stated dataset requirement
and 20 is C6's, and `tests/unit/test_scenarios.py` asserts both against
`claims.md` rather than against what the pack happens to hold.

Nine distinct generative regimes by injected-fault set, the largest supplying
10 of the 40 breaches. Four cargo classes, seven ambient profiles.

| Family | Breach | Control | Notes |
|---|---|---|---|
| compressor degradation | 11 | 2 | includes the flagship |
| door left open | 10 | 2 | the fastest excursions in the pack |
| environmental heat | 6 | 4 | only pharma/vaccine breach; frozen units have the capacity headroom |
| reefer fuel exhaustion | 8 | 4 | cooling stops entirely once the tank empties |
| lying instrument + a real fault | 5 | 6 | three of these produce **no threshold alarm at all** |

The three v1.0.0 scenarios are carried unchanged and keep their digests:

| Scenario | Verified behaviour |
|---|---|
| `compressor_degradation_pharma_01` | In spec for **137 min**, saturates at **86** → 51 min of warning. Axon fires at **102**. Carries the ERP (10.0 °C) vs BOL (8.0 °C) conflict. Vehicle `AX-042`, shipment `SH-2041`. |
| `normal_pharma_run_01` | Never breaches or saturates. The false-alarm control. |
| `sensor_drift_pharma_01` | True temp peaks 5.96 °C, **never breaches**; instrument reports a breach at min 95 with **no fault code**. The multimodal ablation case. |

Golden digests in `tests/unit/test_emitters.py` lock reproducibility for all
60. Changing one is a deliberate act that re-baselines stored results.

**The 57 new scenarios are emitted by `scripts/author_scenario_pack.py`** from
an explicit regime table. Edit the table and regenerate; do not hand-edit a
generated YAML. Each row declares whether it should breach, and the emitter
**refuses to write the pack** if the physics disagrees — which is how a
"sharp failure on a mild day" that the unit actually absorbed was caught
sharing its design with the control row three entries below it.

> **The rule that keeps the pack a dataset and not a result:** rows were tuned
> until the intended breach or non-breach occurred, and **never** after looking
> at what a detector did with them. A pack tuned on detector output makes C1 a
> measurement of the authoring script.

### Two database roles — this matters

`axon` owns the schema and runs migrations. It is a **superuser**, and
PostgreSQL does not apply table privileges to superusers at all, so the
append-only grants on `audit_event` would be silently inert if the application
shared it. Migration `0002` creates `axon_app`, which holds INSERT and SELECT
on that table and nothing more, and the app connects as it.

`poe migrate` must therefore run before the app or the integration tests can
connect at all. An integration test asserts the connected role is **not** a
superuser, because an assertion about grants is worthless without it.

### Not built

Tools layer, OTel/Langfuse wiring. The API layer exposes **read-only control
tower endpoints only** - the B6-B9 services are tested but none of them is
routed, so nothing can be *driven* through HTTP. Approving an action from the
UI is the obvious next thing and is not built.
AxonBench exists and measures four claims; its `rules_llm` arm does not.
**No cassettes are recorded yet.** The provider, the graph and their
guarantees are built and tested, but `data/cassettes/` is empty: recording
needs an API key and a deliberate session. The graph tests supply the two
model nodes as scripted functions, which tests the graph and says nothing
about what a model would produce. **`poe demo` is still rules-only** and is
the unchanged C5 baseline.

---

## 3. Environment

| Fact | Detail |
|---|---|
| Python | 3.12.13 via `uv` (the 3.10 on PATH is irrelevant) |
| Task runner | `poethepoet` — `uv run poe <task>`. **No `make` on this machine.** |
| Docker | Desktop; **it shuts down between sessions — start it first.** Volumes persist, so no reseed needed. |
| ODBC | Only the legacy `SQL Server` driver is installed, not msodbcsql18. `resolve_driver()` handles this; CI installs 18. |
| RAM | 15.7 GB, ~8 GB to the Docker VM. Never run more than the `core` profile locally. |
| Key versions | anthropic **1.7.0**, langgraph **1.2.11**, langgraph-checkpoint-postgres **3.1.2**, pydantic 2.13, sqlglot 30.18 |

> LangGraph is **1.x**, not the 0.2.x most tutorials show. Check the current
> API before writing graph code.

### Session start

```bash
# 1. Start Docker Desktop (it will be down). It is not where you expect:
#    C:\Users\Dilip\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe
# 2. Then:
cd /c/dev/axonfde
docker compose --profile core up -d
uv run poe migrate        # schema + the axon_app runtime role
uv run poe seed           # ERP schema, views, read-only login
uv run poe forge run-all  # scenario recordings (data/generated/ is gitignored)
uv run poe check          # lint, mypy --strict, module contracts, tests
uv run poe forge verify   # simulator determinism + ground-truth agreement
```

To put it on a screen — and this is the sequence to rehearse before showing
anybody, because Docker has died mid-session in four of the last six:

```bash
uv run poe demo-trace     # runs the loop, records what the UI reads
uv run poe tower          # http://localhost:8000
```

`docs/DEMO.md` is the runbook: what to show, in what order, what to say about
the false-alarm rate, and what to do when something is broken on the day.

If a dependency is missing, tests **skip** with the command that fixes it.
To get CI's behaviour instead - a missing dependency is a failure - run:

```bash
AXON_REQUIRE_INTEGRATION=1 uv run poe test
```

A clean run reports **406 passed, 0 skipped** under that flag. Anything
skipped there is a test that is not running in CI either.

---

## 4. Verification

| Command | Checks |
|---|---|
| `uv run poe check` | The full gate. Must be green before any commit. |
| `uv run poe test` | All tests except live-LLM |
| `uv run poe test-sec` | SQL guard and policy bypass — **hard gate, zero tolerance** |
| `uv run poe forge verify` | Every scenario is deterministic and matches its declared ground truth |
| `uv run poe seed` | Reseeds the ERP and self-verifies least privilege |
| `uv run poe migrate` | Builds the app schema and the restricted runtime role |
| `AXON_REQUIRE_INTEGRATION=1 uv run poe test` | What CI runs: skips become failures |
| `AXON_ENV=ci AXON_REQUIRE_INTEGRATION=1 uv run poe check` | **The real CI gate.** Run this before pushing, not plain `poe check` |
| `gitleaks detect --source . --config .gitleaks.toml` | The secret scan, identical to CI's |

---

## 5. Conventions

- **Commits**: conventional prefix, then prose explaining *why*. Record
  anything surprising — especially where a test was wrong. End with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Comments**: explain the non-obvious decision, never restate the code.
  Every threshold and magic number needs its reasoning.
- **Tests**: name the behaviour, not the function. Docstrings explain why the
  property matters. Security tests assert outcomes.
- **Docs**: any number is `PLACEHOLDER` until a benchmark run produces it.
- **Push** at the end of each block: `git push origin main`.

---

## 6. Work blocks

Plan phases in brackets. Each block is one focused session; two in a good one.

### B1 — Persistence foundation ✅ **DONE** (2026-09-19) [Phase 1]
Alembic migrations, append-only `audit_event`, async session factory,
repositories. All six acceptance items met. **Two database roles, not one** —
see §2, it is the part most likely to be undone by accident.

### B2 — Telemetry adapter + incident detection ✅ **DONE** (2026-09-19) [Phase 1]
Parquet → `Evidence`; ERP and seeded-BOL adapters; honest threshold baseline;
incident lifecycle and deduplication. The ERP/BOL conflict is detected from the
live seeded view, and the baseline fires at **minute 137** against the
document's 8 C envelope. Judged against the ERP's 10 C it never fires at all,
which is what makes reconciliation load-bearing rather than tidy.

### B3 — Risk service ✅ **DONE** (2026-09-19) [Phase 1, feeds Phase 3]
Slope extrapolation primary, rule prior as stored baseline, one feature
builder. Fires at **minute 102** on the flagship — 16 after saturation, 35
before breach — and never on the control. `RiskEstimate` cannot be built
without its baseline, which is I3 enforced by the type.

### B4 — Policy engine ✅ **DONE** (2026-09-19) [Phase 1]
All 50 Role x ActionType cells enumerated by test. Deny is absolute including
for Admin; the kill switch is checked before role permission. Bypass rate
zero.

### B5 — Decision engine ✅ **DONE** (2026-09-19) [Phase 1]
Costed candidates with feasibility from real facility data, EV ranking
including `do_nothing`, flip point solved analytically. Recommends
`reroute_to_cold_storage` on the flagship, matching the scenario's declared
correct action, reached independently.

### B6 — Approval + execution + verification ✅ **DONE** (2026-09-20) [Phase 1]
The governance core. All six acceptance items met; 651 tests, 0 skipped under
the CI gate. The binding covers the risk probability **and its baseline** —
removing those fields turns nine tests red, which is how that was confirmed
rather than assumed. Expiry and staleness are distinct codes and a check
reports *every* refusal, not just the first.

### B7 — Rules-only closed loop + demo ✅ **DONE** (2026-09-20) [Phase 1] — **first milestone**
`poe demo`, 13 steps, offline. Detects at minute 102, refuses a live approval
as `APPROVAL_STALE` against 48 readings that really arrived, executes once
under retry, reopens the incident on a failed verification, and verifies the
chain. Rolls back by default — a committing demo works exactly once. Run as
`tests/e2e/test_demo.py` so the ablation arm cannot rot unnoticed.

### B8 — LLM provider + cassettes ✅ **DONE** (2026-09-20) [Phase 1]
`LLMProvider` protocol, Anthropic implementation with prompt caching and
structured outputs, cassettes that raise on a miss, a spend ceiling that
refuses in advance, `ModelInvocation` with cache reads. Confirmed by replacing
the raise with a silent fallback and watching three tests go red.

### B9 — LangGraph workflow ✅ **DONE** (2026-09-21) [Phase 1]
14 nodes, typed checkpointable state, budget gates between nodes, the
deterministic scorer, the deterministic grounding check. Graph runs end to
end, budget exhaustion escalates, a fabricated citation stops the run before
an approval is requested. **Cassettes still to record** — the graph tests use
scripted model nodes, which is stated in the suite rather than implied.

### B10a — AxonBench v0, deterministic claims ✅ **DONE** (2026-09-22) [Phase 1]
Runner, provenance, graders, report, `poe bench` live in CI. **C7 and C8
measured at 0**, with `run-53b8015e9c0b` committed under
`benchmarks/results/published/`. Added an `INSUFFICIENT_DATA` status so a
blocked claim records its shortfall instead of vanishing. Every grader shown
to fail on broken input.

### B10c — Scenario pack v1.1.0 ✅ **DONE** (2026-09-21) [Phase 1]
Pack grown from 3 scenarios to **60** — 40 breach, 20 control, 23 seeded
conflicts — and the **two graders that did not exist** written to consume it.
**C1 measured at 49 min median lead time against a 0.30 false-alarm rate;
C6 at recall 1.00 / precision 1.00.** The block was roughly three times its
brief: see §8 for why, and for the start-of-run false-alarm mode the wider
pack exposed in the shipped detector.

### B15a — Control tower, read-only ✅ **DONE** (2026-09-24) [Phase 6, pulled forward]
`poe demo-trace && poe tower`. A static page over three read-only endpoints:
the trace of the last closed-loop run, the published claim register, and the
telemetry the detectors were judged against. Pulled ahead of its phase because
a demo needs something to look at. **The UI runs nothing** — it reads what a
real run wrote, and reports absence rather than rendering fixtures.
Driving the loop *from* the UI (approve, execute) is not built; that needs the
B6-B9 services routed, which they are not.

### B10b — AxonBench, the LLM arm ← **NEXT** [Phase 1] — **needs one recording session**
Record cassettes for the two model nodes, add the `rules_llm` arm, measure
C5, C9, C12 and C3 against the `rules_only` baseline. `run_arm("rules_llm")`
already refuses with a pointer rather than silently measuring nothing.

### B11 — Streaming [Phase 2]
Redpanda, consumer, event-driven detection, duplicate/out-of-order handling,
lead-time measurement at fleet scale.
**Stop condition:** if consumer groups + replay-from-offset + idempotent
consumption are not all genuinely used, cut Redpanda and write the ADR.

### B12 — Trained risk model [Phase 3]
LightGBM on IncidentForge data, splits by generative regime, isotonic
calibration, reliability diagram, threshold by expected cost.
**Stop condition:** if it does not beat slope extrapolation by ≥5 points on
lead-time-at-fixed-false-alarm-rate after two feature iterations, ship the
baseline and publish the negative result.

Then: B13 multimodal [Phase 4] · B14 AxonRed + failure injection [Phase 5] ·
B15 control tower UI [Phase 6] · B16 AWS [Phase 7] · B17 case study [Phase 8].

**Portfolio-ready checkpoint: end of B13.** Protect it. A finished, measured,
honest system at 70% of scope beats a sprawling 100% attempt.

---

## 7. Next block in detail — B10b (the LLM arm)

**This is the one block that needs a decision before it starts.** Read §0.1.
If there is no `ANTHROPIC_API_KEY` this session, or no willingness to spend
~$2-5 on it, say so at the start and go to B11 or B14 instead. Do not open
this block hoping a key turns up.

### What is already in place
- `LLMProvider`, the Anthropic implementation, prompt caching, structured
  outputs, a daily spend ceiling that refuses *before* sending, and cassettes
  that raise on a miss rather than silently re-recording (B8).
- The 14-node graph, its budget gates, the deterministic scorer and the
  deterministic grounding check (B9). The graph tests supply the two model
  nodes as scripted functions.
- `run_arm("rules_llm")` already refuses with a pointer instead of measuring
  nothing, so the arm cannot accidentally report an empty result.
- `data/cassettes/` is **empty**. That is the whole blocker.

### Deliverables
1. Record cassettes for both model nodes against a handful of scenarios.
   One recording session; every later run replays offline and free.
2. Add the `rules_llm` arm to `poe bench` and to the AxonBench CI job.
3. Measure C5, C9, C12 and C3 against the `rules_only` baseline, which is
   `poe demo` and has not changed since B7.

### Do the claim-method check first, before recording anything
B10a and B10c were both mis-sized because a claim's *metric* reads as
reachable long before its *method* is. Open `claims.md`, read the **Dataset**
and **Method** rows for C3, C5, C9 and C12, and write down how many cases each
needs. Then look at `benchmarks/axonbench/graders/` and count how many of the
four graders exist. In B10c the answer was **zero of two**, while the brief
said the harness "grades whatever the pack contains".

### Watch out for
- **Cost.** The ceiling refuses in advance, but the honest bound is stated in
  `backend/app/llm/spend.py`: overshoot is at most one call's input cost.
- **C9 is prompt injection.** It is a safety gate, so a non-zero result fails
  CI and is a refutation to publish, not a number to tune.
- **The CI job must not need a key.** Cassette replay only. An arm that runs
  nowhere quietly stops being measured, and an arm that spends money on every
  push gets switched off.

### Acceptance
- [ ] Cassettes committed; `poe bench --arm rules_llm` replays offline
- [ ] The `rules_llm` arm runs in CI with no API key
- [ ] Each of C3, C5, C9, C12 either `MEASURED` with its companions or
      `INSUFFICIENT_DATA` with the shortfall recorded — never quietly absent
- [ ] `AXON_ENV=ci AXON_REQUIRE_INTEGRATION=1 uv run poe check` green; pushed;
      **CI badge checked**

## 8. Session log

| Date | Blocks | Outcome |
|---|---|---|
| 2026-09-18/19 | Phase 0, IncidentForge, legacy integration, evidence core, audit chain | 7 commits, 312 tests. Three bugs found by tests, two of which were wrong tests revealing real limitations. |
| 2026-09-19 | **B1 + B2** | 9 commits, 406 tests. Four findings: (1) the app connected as a **superuser**, so the append-only grants were inert — split into `axon`/`axon_app`; (2) concurrent appends **lose events** rather than forking, which is worse for an audit log; (3) the ODBC driver returns DATETIME2 as `str` on this machine and `datetime` in CI; (4) the integration suite was **passing in CI by doing nothing** — no databases were started and every test skipped. |
| 2026-09-19 | **CI repair** | CI had never passed — 8 red runs from commit 1. Four unrelated causes: a config test that could only pass locally, unconfigured gitleaks, an ODBC install pinned to Ubuntu 22.04 on a 24.04 runner, and an empty agent suite making pytest exit 5. |
| 2026-09-19 | **B3 + B4 + B5** | 540 tests. Predictive arm fires at minute 102, 35 min of lead time. Policy matrix complete with zero bypasses. Decision engine agrees with the flagship's declared correct action. Three parameter errors caught by tests, two of them cost models that flattered an action. |
| 2026-09-19 | **CI green** | First passing run in the project's history, run 12. The last cause was gitleaks-action ignoring its own config; replaced with the pinned binary. |
| 2026-09-20 | **B6** | 651 tests. I5 enforced. Two findings: (1) a verification window set from operational intuition (20 min for a phone call) **could not answer its own question** — below an hour the healthy control's slopes overlap the degrading truck's outright, so the floor is now 90 minutes and enforced at construction; (2) a fabricated incident id made the *audit append* fail rather than the execution, so a refused action left **no record** and poisoned the transaction — the executor now resolves the incident first. The security test that found (2) was also wrong: the realistic replay targets a real second incident. |
| 2026-09-20 | **CI repair** | Run 14 red: the security job had no database, and the new I5 gate tests need one. `AXON_REQUIRE_INTEGRATION=1` turned the missing service into a failure rather than a skip — which is the third time that flag has caught a job that would otherwise have gone green while testing less than it claimed. Green on run 15. |
| 2026-09-20 | **B7** | 660 tests. `poe demo` runs the whole loop offline. Four things wrong first: the demo **committed, so it worked exactly once** (second run deduplicated into its own incident and died on a repeated transition) — it now rolls back by default; it also committed **on failure**, leaving half an incident behind; the staleness branch was staged with a **fabricated evidence hash** until the full recording was replayed, which supplies 48 real readings and moves p(breach) 0.621 → 0.687; and `-48 new readings arrived` came from differencing two sliding windows instead of counting arrivals. Verification reports **failed** and reopens the incident, which is honest — the recording is the trajectory of a truck that was not rerouted. |
| 2026-09-20 | **B8** | 692 tests. Cassette misses **raise rather than re-record** — verified by replacing the raise with a silent fallback and watching three tests go red. Spend ceiling refuses *before* sending: output cost is bounded exactly by `max_tokens`, input cost is not knowable locally, so the honest limit is stated — overshoot is at most one call's input cost, never a runaway loop. `cache_read_tokens` stored on every invocation because caching failing is silent. Tenth contract: only `anthropic_provider` imports `anthropic`. |
| 2026-09-21 | **B9** | 736 tests. Four findings: (1) **every observation type the prior rules read did not exist** — `setpoint_temp_c`, `door_open_state`, `reefer_fault_codes` are not declared, nothing raised, every prior sat at its 0.02 floor for ever; now guarded by `PRIOR_OBSERVATION_TYPES` checked at each lookup; (2) **a LangGraph routing function that writes state loses the write** — the budget gate set the reason and every escalation said "without a stated reason"; (3) the grounding check **ignored figures below 10**, exempting every temperature in the system while checking the dollar figures, and reported a correct "78%" as half fabricated; (4) `resolve_envelope` sat in `incidents.replay`, dragging pyodbc into the agent layer — the contract refused and it moved to `evidence/envelope.py`. |
| 2026-09-22 | **B10a** | 762 tests. **C7 and C8 measured at 0**, run committed. Three findings: (1) my own previous estimate of "seven claims reachable" was **wrong — two are**; a claim's *metric* looks reachable long before its *method* is, and the method rows carry dataset requirements (C1 needs 40 scenarios, the pack has 1); (2) the three statuses in `claims.md` had no room for "a grader ran but the dataset is too small", so **`INSUFFICIENT_DATA`** was added — `MEASURED` would be the exact failure I7 prevents and `PLACEHOLDER` discards the run; (3) `benchmarks/results/*.json` was gitignored, so a published number's backing run existed only on one laptop — satisfying the letter of I7 and none of its purpose. Now `published/` is committed. |
| 2026-09-22 | **CI repair** | Run 24 red on the newly enabled AxonBench job: `poe bench -- --arm` forwards the `--` to argparse. The token came from the disabled placeholder step, so it had never run. Green on 25. |
| 2026-09-21 | **B10c** | Pack 3 → **60 scenarios** (40 breach, 20 control, 23 seeded conflicts), two new graders, **C1 and C6 measured**. Five findings, in order of how much they would have cost: (1) **the block's own brief was wrong about its size** — it said five breach scenarios where C1's method says forty, and the contradiction was sitting three lines above it in this file; it also said `poe bench` "grades whatever the pack contains" when **no C1 or C6 grader existed at all**. Re-derive scope from the register, not from the previous summary. (2) The wider pack exposed a **start-of-run false-alarm mode in the shipped detector**: a load begins at setpoint, a proportional controller needs steady-state error to produce output, so in hot ambient the cargo genuinely climbs for ~30 min before levelling — and slope extrapolation cannot tell that curve from an excursion. It fires at **minute 31 regardless of the fault**, sometimes before the fault starts. `CONSECUTIVE_READINGS_TO_FIRE = 3` was tuned on the flagship alone and does not generalise. **Not tuned away** — retuning against the benchmark that measures it is how a number stops meaning anything. (3) A test written to catch a reproducibility bug **passed against the bug**: the C6 negative set used `hash()`, which Python reseeds per process, and my subprocess test compared counts that are identical either way. Fixed by recording the channel assignment and re-proving it red. (4) The emitter's "expect_breach" guard caught a **breach scenario and a control row sharing one design** with opposite labels. (5) A "one regime ≤40% of breaches" test measured regime by *declared root cause* and hid five lying-instrument scenarios behind `compressor_degradation`; `claims.md` means the injected-fault set by "generative regime", and by that measure the largest regime is 25%. |
| 2026-09-24 | **B15a** | Control tower. The demo's `Console` became a `Narrator` protocol with a second implementation that records, so the terminal and the UI run **the same loop over the same databases** rather than two stories that can drift. Three findings: (1) the first UI recovered the two minutes its chart marks by **regex over the narration** — it found the detection minute, missed the threshold alarm, and drew a chart missing the exact comparison the lead-time claim is about, while looking like it had rendered fine. The loop now records them as facts. (2) The API is forbidden from importing pyodbc even transitively, which ruled out running the loop in a request. Left the contract alone and had the demo write a trace the API serves — and the split turned out better anyway, since the loop owns one transaction it rolls back, and holding that open across an HTTP request would be a worse design than the one the contract forced. (3) The preamble note stole step number 1, so the UI said "step 2" where the terminal said "step 1". Numbering now counts titled steps. The I8 test (no ground truth through the API) was proven red by leaking `true_cargo_temp_c` on purpose. (4) The comparison panel's slopes were endpoint differences, reporting **-8.7 rpm/min where `claims.md` records -9.2** for the same scenario and window. Both are "the slope"; nothing on either side said which. Fixed to a least-squares fit (what `risk/features.py` uses) — still 0.4 out, because the register's "30-minute window at minute 100" means the thirty readings **ending** at 100 (71–100), not an inclusive 70–100, which is thirty-one. **A window's boundary convention was worth four tenths of a rpm/min**, and a UI quoting a number the register contradicts is worse than no UI. All four figures now match because they are the same calculation, not copied values. **Known gap: the JavaScript has no automated test** — it was verified once by executing its render functions against the live API in a DOM shim, and nothing guards it in CI. |
| | **B10b next** | The LLM arm. **Needs an API key and a decision about spending** — see §0.1 and §7. If there is no key, go to B11 or B14 instead. |
