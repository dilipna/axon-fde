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
| I5 | **No consequential action executes without a valid, unexpired, hash-matching approval.** | To be enforced in Block B6 |
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

**A gate nobody looks at is not a gate.** CI had failed on **all eight runs
since the first commit**, and it went unnoticed because `poe check` is green
locally. Four unrelated causes, all pre-existing. The one worth remembering:
`Settings(_env_file=None)` stops pydantic reading a `.env` file but **not**
`os.environ`, so a test asserting "the default environment is local" passed on
every laptop and failed on every CI run, where `AXON_ENV=ci` is exported.
Check the badge after pushing.

Write tests that would fail if the claim were false. Prefer asserting outcomes
over mechanisms.

---

## 2. Current state

**11 commits · 406 tests · mypy --strict clean · 7 module contracts · pushed to
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

Risk service, policy engine, decision engine, approvals, action executor,
verification, LangGraph workflow, LLM provider + cassettes, tools layer,
AxonBench, the demo script, OTel/Langfuse wiring, any UI.

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

### B3 — Risk service ← **NEXT** [Phase 1, feeds Phase 3]
`RiskService` interface; rule baseline + slope-extrapolation baseline; feature
builder shared by training and serving; `RiskAssessment` persisted with its
baseline.
**Done when:** the flagship scenario yields a rising risk score that crosses
threshold **before** minute 137, with lead time computed against the baseline.

### B4 — Policy engine [Phase 1]
Pure function, full role × action matrix from `docs/architecture/overview.md`.
Deny is absolute, including for Admin.
**Done when:** every matrix cell is tested and the bypass rate is zero.

### B5 — Decision engine [Phase 1, deepened in Phase 3]
Rule-driven action catalogue with feasibility from facility data; expected
value including `do_nothing`; decision-flip sensitivity.
**Done when:** the flagship scenario produces ≥3 costed candidates plus
`do_nothing`, ranked by EV, with the flip point reported.

### B6 — Approval + execution + verification [Phase 1] — *the governance core*
Hash-bound approvals with expiry; idempotent simulated executors; scheduled
verification; audit events for every transition.
**Done when:** approving against mutated evidence yields `APPROVAL_STALE`,
re-executing the same approval is a no-op, and a failed verification reopens
the incident.

### B7 — Rules-only closed loop + demo [Phase 1] — **first milestone**
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

## 7. Next block in detail — B3

### Already measured — do not re-derive this

A slope-extrapolation prototype was written, run against all three recordings,
and then **deleted rather than committed half-finished**. The measurements are
the valuable part and they are below. The rule is: fit `cargo_temp_c` over a
trailing window by least squares, project to the envelope, and fire when the
projected breach falls inside a 60-minute horizon.

| Window | Flagship first fires | Verdict |
|---|---|---|
| 15 min | **minute 15** | Fits sensor scatter. 71 minutes before the unit is in any trouble. |
| **30 min** | **minute 100** | 14 min after saturation (86), **37 min before breach** (137). |
| 45 min | minute 103 | Same answer, three minutes of lead time thrown away. |

`normal_pharma_run_01` never fires at any window — the false-alarm control
holds. Slopes there stay under 0.005 °C/min against 0.024 for the flagship.

**30 minutes is the shortest window that does not fit noise.** Use it, and
keep the table above in the docstring so the number is justified rather than
chosen.

### Two findings that change what B3 should claim

**1. Slope extrapolation false-alarms on `sensor_drift`, and does it early.**
With a 30-minute window it fires at **minute 56** — earlier and more
confidently than the threshold baseline, which fires around minute 95 when the
reported temperature crosses 8 C. The true temperature never leaves spec.
Predicting harder on a lying sensor means being confidently wrong sooner. That
is the honest B3 result for this scenario and it should be recorded, not
tuned away.

**2. `sensor_drift` is separable from telemetry alone — this threatens C4.**
Measured at minute 100, 30-minute window:

| Scenario | temp slope | rpm slope | fault codes |
|---|---|---|---|
| compressor_degradation | +0.016 | **−9.2** | `AL17` |
| sensor_drift | +0.039 | **+2.0** | none |
| normal | +0.002 | +2.4 | none |

A compressor winding **down** while cargo warms is a unit losing the fight. A
compressor winding **up** while the reported temperature climbs fast is
physically incoherent — the sensor is lying. Either that inconsistency or the
plain absence of a fault code separates the three cases **without any second
modality**.

C4 in `docs/evaluation/claims.md` names `sensor_drift` as the family "where
visual evidence should be decisive". If the Phase 4 ablation does not control
for the compressor-response feature, it will credit the photograph with a
discrimination that single-modality telemetry already achieves. **Add the
caveat to C4 before running that ablation.** Do not fix it by removing the
feature — the feature is real and useful; fix it by controlling for it.

### Deliverables
1. `backend/app/risk/features.py` — **one** feature builder, shared by
   training and serving. Two would drift, and the drift would present as a
   model regression. Include `compressor_rpm_slope`; B12 needs it and finding
   2 above is why.
2. `backend/app/risk/baselines.py` — `RuleBaseline` (static prior from
   headroom + fault codes) and `SlopeExtrapolation` (the table above). The
   second is what B12 must beat by ≥5 points or the trained model does not
   ship, so write it to be genuinely good.
3. `backend/app/risk/service.py` — `RiskService` protocol returning a
   probability **and** its baseline, always, in one object.
4. `PredictiveDetector` in `incidents/detection.py`, implementing the existing
   `Detector` protocol. Pass both detectors the same window: `BaselineDetector`
   already ignores everything but the latest reading, so identical inputs is
   the fair arrangement and needs no special case.
5. `RiskAssessment` persisted with `baseline_probability`, `model_version`,
   `feature_vector_hash`, `degraded`; a repository alongside the other three.
6. Lead time measured between the two arms and recorded.

### Watch out for
- **These are not calibrated probabilities.** A logistic on time-to-breach is
  a monotone score, not a frequency. Say so in the docstring, and keep C2 at
  `PLACEHOLDER` until B12's isotonic regression and reliability diagram exist.
  Nothing may multiply one of these by a cargo value to get an expected loss.
- **`baseline_probability` is NOT NULL** (invariant I3). Do not add a code
  path that stores a prediction without its baseline.
- **Ground truth must not reach the feature builder** (invariant I8). Add an
  import-linter contract forbidding `backend` from importing `simulator`,
  where `GroundTruthFrame` lives. Cheap, and it makes I8 structural.
- **Both detectors share a `correlation_key`**, so whichever runs second
  deduplicates into the other's incident. Run the arms as **separate replays**
  rather than putting `detected_by` in the key — in production only one
  detector runs, and a key that splits by detector would produce two incidents
  for one truck, which is the exact failure deduplication exists to prevent.
- **A slope fit on a saturating curve is not a straight line.** The projection
  is conservative here, which is the safe direction, but it is wrong either
  way — worth a comment rather than a silent assumption.

### Acceptance
- [ ] Flagship risk crosses threshold **before minute 137 and after minute 86**
      (expect 100 with a 30-minute window)
- [ ] Lead time measured against the baseline arm and recorded
- [ ] `normal_pharma_run_01` never crosses the threshold
- [ ] `sensor_drift_pharma_01`'s false alarm is asserted by a test, with a
      docstring saying it is the expected Phase 1 result
- [ ] Every `RiskAssessment` has a non-null `baseline_probability`
- [ ] C4 in `claims.md` carries the single-modality caveat
- [ ] `AXON_ENV=ci AXON_REQUIRE_INTEGRATION=1 uv run poe check` green; pushed;
      **CI badge checked**

## 8. Session log

| Date | Blocks | Outcome |
|---|---|---|
| 2026-09-18/19 | Phase 0, IncidentForge, legacy integration, evidence core, audit chain | 7 commits, 312 tests. Three bugs found by tests, two of which were wrong tests revealing real limitations. |
| 2026-09-19 | **B1 + B2** | 9 commits, 406 tests. Four findings: (1) the app connected as a **superuser**, so the append-only grants were inert — split into `axon`/`axon_app`; (2) concurrent appends **lose events** rather than forking, which is worse for an audit log; (3) the ODBC driver returns DATETIME2 as `str` on this machine and `datetime` in CI; (4) the integration suite was **passing in CI by doing nothing** — no databases were started and every test skipped. |
| 2026-09-19 | **CI repair** | CI had never passed — 8 red runs from commit 1. Four unrelated causes: a config test that could only pass locally, unconfigured gitleaks, an ODBC install pinned to Ubuntu 22.04 on a 24.04 runner, and an empty agent suite making pytest exit 5. |
| | **B3 next** | Risk service. §7 carries measurements already taken: 30-minute window fires at minute 100. Two findings there change what B3 may claim. |
