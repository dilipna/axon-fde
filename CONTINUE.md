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
| I8 | **Ground truth never reaches the application.** `TelemetryEvent` carries only what a sensor could report; `GroundTruthFrame` is benchmark-only. | `test_telemetry_events_carry_no_ground_truth`, `test_written_telemetry_contains_no_ground_truth` |
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

**21 commits · 651 tests · mypy --strict clean · 9 module contracts · **CI green on all five jobs** · pushed to
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
| Config / health / logging | `backend/app/config.py`, `api/v1/health.py`, `observability/logging.py` | Startup safety validation, TCP dependency probes, redaction |

### Scenario pack (`data/scenarios/pack_v1`)

| Scenario | Verified behaviour |
|---|---|
| `compressor_degradation_pharma_01` | In spec for **137 min**, saturates at **86** → 51 min of warning. Carries the ERP (10.0 °C) vs BOL (8.0 °C) conflict. Vehicle `AX-042`, shipment `SH-2041`. |
| `normal_pharma_run_01` | Never breaches or saturates. The false-alarm control. |
| `sensor_drift_pharma_01` | True temp peaks 5.96 °C, **never breaches**; instrument reports a breach at min 95 with **no fault code**. The multimodal ablation case. |

Golden digests in `tests/unit/test_emitters.py` lock reproducibility. Changing
them is a deliberate act that re-baselines stored results.

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

LangGraph workflow, LLM provider + cassettes, tools layer, AxonBench, the
demo script, OTel/Langfuse wiring, any UI. The API layer exposes none of B6
yet - the services exist and are tested, but nothing is routed.

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

### B7 — Rules-only closed loop + demo ← **NEXT** [Phase 1] — **first milestone**
Wire B1–B6 into a runnable loop with **no LLM**. `poe demo`, 13 steps,
including the `APPROVAL_STALE` branch.
**Done when:** `poe demo` runs the whole loop offline and the audit chain
verifies at the end. *This is the `rules_only` ablation arm (claim C5) —
obtained for free by building rules-first.*

### B8 — LLM provider + cassettes [Phase 1]
`LLMProvider` protocol, Anthropic implementation with structured outputs and
prompt caching, record/replay cassettes, spend ceiling, `ModelInvocation`.
**Done when:** tests replay cassettes offline and a cassette mismatch **fails
loudly** rather than silently re-recording.

### B9 — LangGraph workflow [Phase 1]
14 nodes, typed state, Postgres checkpointer, budget guards, hypothesis scorer
(rules propose priors, LLM proposes links, **deterministic scorer owns every
confidence**), deterministic grounding check.
**Done when:** the graph runs end to end, budget exhaustion escalates, and the
grounding check rejects a fabricated citation.

### B10 — AxonBench v0 [Phase 1] — **claims become real**
8 scenarios, ~12 deterministic graders, arms (`rules_only` vs `rules+llm`),
result persistence with full provenance, CI regression gate.
**Done when:** `poe bench` runs in CI and `poe bench report` generates the
markdown the README embeds. Replace the first `PLACEHOLDER`s in `claims.md`.

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

## 7. Next block in detail — B7

The first milestone: the whole thing running end to end, offline, with no LLM.
Everything it needs now exists — B6 was the last missing piece.

### What is already in place
- `ScenarioReplay` runs telemetry → evidence → reconciliation → detection and
  owns its transaction.
- `SlopeRiskService.assess` returns a `RiskEstimate` carrying its baseline.
- `rank_options` costs every candidate and carries the policy verdicts.
- `ApprovalService` / `ActionExecutor` / `VerificationService` close the loop,
  and every transition writes an audit event.

### Deliverables
1. `scripts/demo.py` — 13 steps, `poe demo`, no network and no model.
2. The `APPROVAL_STALE` branch on screen: grant an approval, inject a reading,
   watch the execution refuse and a fresh approval be sought.
3. A closing audit-chain verification printed as the last step.

### Watch out for
- **The demo is the `rules_only` ablation arm for claim C5.** It is obtained
  for free by building rules-first, but only if it records its results in the
  same shape AxonBench will read. Decide that shape now, not in B10.
- `ExecutionOutcome.expected_effect` is deliberately `None` on a replay, so a
  demo that schedules verification from a retried execution has nothing to
  schedule. That is the intended shape; the loop must not paper over it.
- The verification window for a reroute is 90 minutes of *scenario* time. The
  demo must drive a clock rather than sleep.

### Acceptance
- [ ] `poe demo` runs the whole loop offline and prints 13 steps
- [ ] The `APPROVAL_STALE` branch is exercised, not described
- [ ] The audit chain verifies at the end
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
| | **B7 next** | Rules-only closed loop and `poe demo` — the first end-to-end milestone, and the `rules_only` ablation arm for C5. |
