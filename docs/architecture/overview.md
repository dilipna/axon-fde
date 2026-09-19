# System architecture

For *why* this shape rather than another, see the [ADRs](../adr/README.md).
For the problem it solves, see the [customer brief](../customer-brief.md).

---

## 1. Shape

A **modular monolith** (one FastAPI application with linter-enforced internal
boundaries), plus two processes separated by lifecycle rather than by domain:

| Process | Lifecycle | Phase |
|---|---|---|
| `api` | Request/response, serves the control tower | 1 |
| `consumer` | Continuously consumes the telemetry stream; must survive an API deploy without losing its offset | 2 |
| `worker` | Scheduled outcome verification and benchmark runs | 1 |

All three import the same modules. See [ADR-002](../adr/002-modular-monolith.md).

---

## 2. Layers

```
┌────────────────────────────────────────────────────────────────────┐
│ CLIENT   Next.js control tower · 5 screens · Legacy Mode toggle    │
│          OIDC PKCE · TypeScript client generated from OpenAPI      │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ HTTPS + JWT
┌──────────────────────────────▼─────────────────────────────────────┐
│ API       FastAPI · Pydantic v2 · OpenAPI 3.1                      │
│           AuthN: verify JWT → Principal(sub, role, scopes)         │
│           AuthZ: dependency → PolicyEngine.authorize(...)          │
│           Thin: validates, authorises, delegates. No logic.        │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
     ┌─────────────────────────┼──────────────────────────┐
     │                         │                          │
┌────▼─────────┐   ┌───────────▼────────────┐  ┌──────────▼─────────┐
│ ORCHESTRATION│   │ DOMAIN SERVICES        │  │ GOVERNANCE         │
│              │   │                        │  │                    │
│ LangGraph    │◄──┤ EvidenceService        │  │ PolicyEngine       │
│  investigate │   │ ReconciliationService  │  │  (pure, no I/O)    │
│  verify      │   │ HypothesisScorer       │  │ ApprovalService    │
│              │   │ RiskService (ML)       │  │  (hash-bound)      │
│ Postgres     │   │ DecisionEngine (EV)    │  │ ActionExecutor     │
│ checkpointer │   │ WhatIfSimulator        │  │  (idempotent)      │
│ budget guards│   │ ContextAssembler       │  │ AuditLog (chained) │
└────┬─────────┘   └───────────┬────────────┘  └──────────┬─────────┘
     │                         │                          │
┌────▼─────────────────────────▼──────────────────────────▼─────────┐
│ TOOLS     typed · policy-gated · traced · budget-counted          │
│  read     shipment · vehicle · cargo_requirements · telemetry ·   │
│           maintenance · facilities · weather · knowledge          │
│  compute  calculate_risk · simulate_intervention                  │
│  extract  analyze_image · extract_document          (Phase 4)     │
│  write    create_incident · propose_action · execute_approved     │
└────┬───────────────────────────────────────────────────────────────┘
     │
┌────▼───────────────────────────────────────────────────────────────┐
│ INTEGRATION                                                        │
│  LegacyRepo  → SQL Server 2022   read-only login, 6 vw_ai_* views, │
│                                  SQLGlot AST allowlist, timeouts   │
│  AppRepo     → PostgreSQL 16 + pgvector                            │
│  ObjectStore → MinIO / S3        artifacts, images, model files    │
│  EventBus    → Redpanda          (Phase 2)                         │
│  LLM/VLM     → provider protocol (Anthropic default)               │
└────┬───────────────────────────────────────────────────────────────┘
     │
┌────▼───────────────────────────────────────────────────────────────┐
│ SIMULATION & EVALUATION                                            │
│  IncidentForge     seeded scenarios · thermal ODE · fault injection│
│  BaselineDetector  threshold alarms on the same stream             │
│  AxonBench         deterministic graders · modality and LLM arms   │
│  AxonRed           adversarial scenario pack                       │
└────┬───────────────────────────────────────────────────────────────┘
     │
┌────▼───────────────────────────────────────────────────────────────┐
│ OBSERVABILITY  OpenTelemetry → OTLP · Langfuse for LLM spans ·     │
│                structlog JSON · incident_id correlates everything  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. Where the LLM is, and is not

This is the defining property of the architecture.

| The LLM **may** | The LLM **may never** |
|---|---|
| Propose root-cause hypotheses | Author an observation |
| Propose candidate actions | Emit a confidence score |
| Link evidence to hypotheses, citing IDs | Compute a risk probability |
| Compose the narrative explanation | Decide a policy outcome |
| Fill gaps in the evidence plan (optional) | Cause a side effect |

Of fourteen workflow nodes, **ten are fully deterministic**, two are hybrid,
one is LLM, one is optionally LLM. That ratio is deliberate, and claim C5
measures whether the LLM's share earns its place.

### How the restrictions are enforced

| Restriction | Mechanism |
|---|---|
| No LLM-authored evidence | No LLM member in `EvidenceSource`; evidence service rejects; test asserts |
| No LLM risk or EV arithmetic | Import-linter: `decision` may not import `anthropic` or `app.llm` |
| No LLM policy decisions | Import-linter: `policies` may not perform I/O |
| No LLM side effects | Model output is an `ActionCandidate` (data); execution needs a valid approval |
| No unsupported claims | Grounding check validates citations and numbers against the bundle |

---

## 4. The two workflows

### Investigation

```
triage → plan_evidence → gather_evidence → reconcile → assess_risk
      → form_hypotheses → generate_actions → simulate_and_score
      → compose_recommendation → grounding_check → policy_check
      → await_approval ⟳ → execute
```

`await_approval` is a **graph interrupt**: state is checkpointed to PostgreSQL,
the process may restart, and the run resumes when a human decides. This is the
specific capability LangGraph was chosen for (ADR-003).

Global guards — 25 steps, 40 tool calls, 180 s wall clock, 120k tokens — each
abort deterministically to `ESCALATED` rather than letting a run spin.

### Verification

A **separate** graph, triggered by the scheduler at `t + window`:

```
load_incident → collect_post_action_telemetry → evaluate_outcome
             → { RESOLVED | REOPENED | ESCALATED }
```

Separate because holding a graph run open for thirty minutes is a production
anti-pattern: it does not survive a restart and it ties up a worker. On reopen,
the failed hypothesis and outcome enter the next investigation's context, so
the system does not silently retry a diagnosis that already failed.

---

## 5. Data stores

| Store | Owns | Access |
|---|---|---|
| **SQL Server 2022** | Axon's ERP — shipments, vehicles, routes, maintenance | Read-only, six views, never migrated by us |
| **PostgreSQL 16 + pgvector** | Incidents, evidence, hypotheses, approvals, audit chain, embeddings, agent checkpoints | Full ownership, Alembic-managed |
| **MinIO / S3** | Artifacts: photographs, PDFs, model files, scenario outputs | Content-addressed by sha256 |
| **Redpanda** (Phase 2) | `telemetry.raw`, `telemetry.enriched`, `incidents.lifecycle` | Replay-from-offset is the justification |

The asymmetry between the first two is the integration story: we treat the ERP
as someone else's system because it is.

---

## 6. Security posture

| Concern | Control |
|---|---|
| Legacy write access | Impossible: the grant has none ([ADR-005](../adr/005-read-only-legacy-access.md)) |
| SQL abuse | AST allowlist + grant + timeout + row cap ([ADR-006](../adr/006-sql-parsed-before-execution.md)) |
| Unauthorised actions | Deterministic policy engine outside the model |
| Approval replay | Hash-bound, expiring, single-use approvals |
| Duplicate execution | Idempotency keys with a unique constraint |
| Audit tampering | Append-only grants + trigger + hash chain, detectable even if the DB is compromised |
| Data leakage | Repository-layer principal filtering — restricted rows never enter a context bundle |
| Prompt injection | Retrieved content is data, never instructions; no tool call is authorised by model text alone |

Restricted data is filtered **before** it reaches the model. There is no
instruction anywhere telling the model not to reveal something it was given.

Full analysis: [threat model](../threat-model/README.md).

---

## 7. Degradation ladder

| Mode | Trigger | Behaviour |
|---|---|---|
| `FULL` | — | Everything available |
| `NO_VLM` | Vision provider down | Visual evidence absent, noted in the bundle |
| `NO_LLM` | LLM down, rate-limited, or refusing | Rule hypotheses, rule actions, templated narrative |
| `DETECT_ONLY` | Multiple dependencies down | Detect and escalate; no recommendation |

**Universal rule: never fabricate a missing observation.** Absent evidence is
represented as absent, is visible in the UI, and lowers confidence. A degraded
answer that admits its gaps is the product; a confident answer over missing
data is the failure mode this system exists to remove.

---

## 8. Current state

| Component | Status |
|---|---|
| Observation taxonomy + typed loader | **Built** (Phase 0) |
| Configuration with startup safety validation | **Built** (Phase 0) |
| Health and dependency probes | **Built** (Phase 0) |
| Structured logging with correlation | **Built** (Phase 0) |
| Module boundary contracts (5) | **Enforced** (Phase 0) |
| Legacy schema, views, restricted grant | Phase 1 |
| Evidence persistence + reconciliation | Phase 1 |
| Policy engine, approvals, audit chain | Phase 1 |
| Investigation and verification graphs | Phase 1 |
| AxonBench v0 | Phase 1 |
| Streaming, IncidentForge, baseline detector | Phase 2 |
| Trained risk model, EV decisions, what-if | Phase 3 |
| Vision, documents, cross-modal conflicts | Phase 4 |
