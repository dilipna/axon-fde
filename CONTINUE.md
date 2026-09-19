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
| I1 | **A language model can never be the source of an observation.** It links, interprets and narrates evidence; it never creates it. | No LLM member in `EvidenceSource`; DB CHECK `ck_evidence_source_never_llm`; `test_llm_is_not_a_valid_evidence_source` |
| I2 | **Confidence is computed, never supplied.** Single implementation in `Taxonomy.confidence()`. No constructor accepts a caller-provided confidence. | `Evidence.create()` is the only construction path |
| I3 | **Risk probability is computed outside the LLM**, and always stored beside its non-ML baseline. | `RiskAssessment.baseline_probability` is `NOT NULL`; import-linter forbids `decision` importing `anthropic` |
| I4 | **The AI cannot write to the legacy ERP.** Read-only login, six views, AST allowlist, timeouts. | `tests/integration/test_legacy_access.py`, `tests/security/test_sql_guard.py` |
| I5 | **No consequential action executes without a valid, unexpired, hash-matching approval.** | To be enforced in Block B6 |
| I6 | **Never fabricate a missing observation.** Absence is represented as absence and lowers confidence. | Degradation ladder, `docs/architecture/overview.md` §7 |
| I7 | **No number ships without a stored benchmark run behind it.** | `docs/evaluation/claims.md` |
| I8 | **Ground truth never reaches the application.** `TelemetryEvent` carries only what a sensor could report; `GroundTruthFrame` is benchmark-only. | `test_telemetry_events_carry_no_ground_truth`, `test_written_telemetry_contains_no_ground_truth` |
| I9 | **Policy and decision modules are pure** — no I/O, no LLM. | import-linter contracts in `pyproject.toml` |

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

Write tests that would fail if the claim were false. Prefer asserting outcomes
over mechanisms.

---

## 2. Current state

**7 commits · 312 tests · mypy --strict clean · 6 module contracts · pushed to
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
| App schema models | `backend/app/db/app/models.py` | 12 tables — **no migration yet** |
| Config / health / logging | `backend/app/config.py`, `api/v1/health.py`, `observability/logging.py` | Startup safety validation, TCP dependency probes, redaction |

### Scenario pack (`data/scenarios/pack_v1`)

| Scenario | Verified behaviour |
|---|---|
| `compressor_degradation_pharma_01` | In spec for **137 min**, saturates at **86** → 51 min of warning. Carries the ERP (10.0 °C) vs BOL (8.0 °C) conflict. Vehicle `AX-042`, shipment `SH-2041`. |
| `normal_pharma_run_01` | Never breaches or saturates. The false-alarm control. |
| `sensor_drift_pharma_01` | True temp peaks 5.96 °C, **never breaches**; instrument reports a breach at min 95 with **no fault code**. The multimodal ablation case. |

Golden digests in `tests/unit/test_emitters.py` lock reproducibility. Changing
them is a deliberate act that re-baselines stored results.

### Not built

Persistence (no Alembic migration), repositories, audit service, telemetry
adapter, risk service, policy engine, approvals, action executor, verification,
LangGraph workflow, LLM provider + cassettes, tools layer, AxonBench, the demo
script, OTel/Langfuse wiring, any UI.

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
# 1. Start Docker Desktop (it will be down)
# 2. Then:
cd /c/dev/axonfde
docker compose --profile core up -d
uv run poe check          # lint, mypy --strict, module contracts, tests
uv run poe forge verify   # simulator determinism + ground-truth agreement
```

If SQL Server is unreachable, integration tests **skip** rather than fail.
If they fail instead, a test is opening its own connection without the
`legacy_ready` fixture.

---

## 4. Verification

| Command | Checks |
|---|---|
| `uv run poe check` | The full gate. Must be green before any commit. |
| `uv run poe test` | All tests except live-LLM |
| `uv run poe test-sec` | SQL guard and policy bypass — **hard gate, zero tolerance** |
| `uv run poe forge verify` | Every scenario is deterministic and matches its declared ground truth |
| `uv run poe seed` | Reseeds the ERP and self-verifies least privilege |

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

### B1 — Persistence foundation ← **NEXT** [Phase 1]
Alembic migration for the 12 models; append-only enforcement on `audit_event`
(role grants + `BEFORE UPDATE OR DELETE` trigger); async session factory;
repositories for evidence, incident, audit.
**Done when:** `poe migrate` builds the schema; an integration test proves
`UPDATE`/`DELETE` on `audit_event` fails at the database; evidence round-trips.

### B2 — Telemetry adapter + incident detection [Phase 1]
IncidentForge Parquet → `Evidence` rows. Legacy repo → cargo requirement
evidence. Seeded BOL extraction fixture. Threshold `BaselineDetector`. Incident
lifecycle state machine + deduplication.
**Done when:** replaying the flagship scenario produces persisted evidence, the
ERP/BOL conflict is detected **from real seeded data**, and the baseline
detector fires at minute 137 while Axon's own detection is still pending.

### B3 — Risk service [Phase 1, feeds Phase 3]
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

## 7. Next block in detail — B1

### Deliverables
1. `backend/app/db/app/session.py` — async engine + session factory (asyncpg).
2. `backend/app/db/migrations/` — Alembic env wired to `models.Base.metadata`,
   reading the DSN from settings.
3. Migration `0001_initial` — all 12 tables.
4. Migration `0002_audit_append_only` — revoke `UPDATE`/`DELETE` on
   `audit_event` from the app role, plus a `BEFORE UPDATE OR DELETE` trigger
   that raises.
5. `backend/app/db/app/repositories/{evidence,incident,audit}.py`.
6. `backend/app/audit/service.py` — appends with correct `seq`/`prev_hash`
   under a transaction, so two concurrent appends cannot fork the chain.
7. `AXON_AUDIT_HMAC_KEY` in config and `.env.example`.
8. `tests/integration/test_app_db.py`.

### Watch out for
- **Chain append is a concurrency problem.** Two simultaneous appends could
  read the same head and fork. Use `SELECT ... FOR UPDATE` on the head row or
  a table-level advisory lock, and **write a test that appends concurrently**.
- `pgvector` extension: `CREATE EXTENSION IF NOT EXISTS vector` in the first
  migration (the image ships it; it still needs enabling).
- The trigger must be created as a superuser but must fire for the app role.
- Keep the async app DB separate from the sync legacy path — the import-linter
  contract already forbids leakage.

### Acceptance
- [ ] `poe migrate` builds the schema from empty
- [ ] `UPDATE audit_event` fails at the database, proven by an integration test
- [ ] `DELETE FROM audit_event` fails likewise
- [ ] Evidence round-trips with value, provenance and confidence intact
- [ ] Concurrent chain appends produce a single valid chain
- [ ] `poe check` green; committed and pushed

---

## 8. Session log

| Date | Blocks | Outcome |
|---|---|---|
| 2026-09-18/19 | Phase 0, IncidentForge, legacy integration, evidence core, audit chain | 7 commits, 312 tests. Three bugs found by tests, two of which were wrong tests revealing real limitations. |
| | **B1 next** | |
