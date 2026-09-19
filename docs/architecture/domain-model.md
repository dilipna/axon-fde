# Domain model

Two stores, owned very differently.

**The legacy system (SQL Server)** is Axon's existing ERP. We do not own its
schema, we never migrate it, and we never write to it. We read six approved
views through a login that holds `SELECT` on those views and nothing else.

**The application database (PostgreSQL)** is ours. It holds everything the
incident loop produces: evidence, hypotheses, risk assessments, candidate
actions, approvals, executions, verifications and the audit chain.

That asymmetry is the integration story. It is also a constraint that shapes
every entity below: anything AxonFDE learns about the world lives on our side,
linked back to the legacy record by identifier, never by modifying it.

---

## 1. Legacy entities (read-only)

```
Customer ──< Shipment >── Vehicle ──< MaintenanceEvent
                │            │
                │            └──< HistoricalIncident
                ├── CargoRequirement
                ├── Route ──< RouteStop >── Facility
                └──< ShipmentDocumentRef
```

| Entity | Key fields | Notes |
|---|---|---|
| `Customer` | `customer_id`, `name`, `tier`, `notification_contact` | Tier drives notification policy |
| `Vehicle` | `vehicle_id`, `reefer_model`, `trailer_class`, `in_service_date` | `trailer_class` selects thermal parameters |
| `Driver` | `driver_id`, `name`, `phone`, `certifications` | Cold-chain certification gates some actions |
| `Shipment` | `shipment_id`, `customer_id`, `vehicle_id`, `driver_id`, `route_id`, `status`, `cargo_value_usd` | `cargo_value_usd` feeds the expected-value layer |
| `CargoRequirement` | `shipment_id`, `cargo_class`, `permitted_temp_min_c`, `permitted_temp_max_c`, `special_handling` | **The ERP side of the BOL conflict** |
| `Route` | `route_id`, `origin`, `destination`, `planned_duration_min` | |
| `RouteStop` | `route_id`, `sequence`, `facility_id`, `planned_arrival` | |
| `Facility` | `facility_id`, `name`, `lat`, `lon`, `capabilities`, `capacity_slots` | Reroute feasibility depends on `capabilities` |
| `MaintenanceEvent` | `event_id`, `vehicle_id`, `occurred_at`, `event_type`, `fault_codes`, `technician_notes`, `severity` | Source of the prior AL17 warning |
| `HistoricalIncident` | `incident_id`, `vehicle_id`, `symptom_summary`, `root_cause`, `intervention`, `outcome` | Seeds institutional memory (Phase 5) |
| `ShipmentDocumentRef` | `shipment_id`, `doc_type`, `storage_uri`, `signed_at` | Pointer only; bytes live in the object store |

### Exposed views

Only these are readable by the application login. Each is a projection, not
`SELECT *` — columns the AI has no business seeing (pricing terms, driver
personal data) never leave the database.

`vw_ai_shipments` · `vw_ai_vehicles` · `vw_ai_cargo_requirements` ·
`vw_ai_maintenance` · `vw_ai_facilities` · `vw_ai_historical_incidents`

---

## 2. Application entities

### 2.1 The evidence core

```
Artifact ──< Evidence >──< EvidenceLink >── Evidence
                │
                ├──< HypothesisEvidence >── Hypothesis
                └── (cited by) Recommendation
```

**`Artifact`** — immutable bytes plus metadata. A photograph, a PDF, a CSV
drop, a model artifact. `sha256` makes it addressable and makes tampering
detectable. Evidence extracted from an artifact keeps a reference to it, so a
reviewer can always open the exact image behind a claim.

**`Evidence`** — one typed observation. This is the schema every modality
normalises into, and it is why contradiction detection is deterministic. Fully
specified in [`evidence-model.md`](evidence-model.md); the load-bearing fields
are `observation_type` (from the taxonomy), `value`, `source`, `observed_at`,
`valid_from`/`valid_to`, `confidence`, `freshness`, `content_hash`, `status`.

> **Invariant:** `source` can never be a language model. The LLM links,
> interprets and narrates evidence; it never authors an observation. Enforced
> by the `EvidenceSource` enum, by the evidence service, and by a test.

**`EvidenceLink`** — a typed relation between two pieces of evidence:
`corroborates`, `contradicts`, or `supersedes`. A separate table rather than
array columns, because "show every open conflict in the fleet" is a first-class
query, not an incident-scoped one.

> `supersedes` is deliberately distinct from `contradicts`. A newer reading
> from the *same* source replaces an older one; that is not a disagreement
> between sources and must not be shown to a dispatcher as a conflict.

### 2.2 The incident loop

```
Incident ──< Hypothesis
    │
    ├──< RiskAssessment
    ├──< ActionCandidate ──┐
    ├──  Recommendation ───┤
    │         │            │
    │         └── Approval ─┴──> ActionExecution ──> OutcomeVerification
    │
    └──< AuditEvent (append-only, hash-chained)
```

| Entity | Purpose | Fields that matter |
|---|---|---|
| `Incident` | The unit of work | `correlation_key`, `status`, `severity`, `entity_ref`, `detected_by` (`axon`\|`baseline`\|`human`), `predicted_breach_at`, `superseded_by`, `scenario_run_id` |
| `Hypothesis` | A candidate root cause | `cause_code`, `prior`, `posterior_confidence`, `scorer_version`, `status` |
| `HypothesisEvidence` | Evidence→hypothesis link | `stance` (`supports`\|`contradicts`), `weight`, `source` (`rule`\|`llm`) |
| `RiskAssessment` | A probability with its provenance | `probability`, **`baseline_probability`**, `model_version`, `calibration_version`, `feature_vector_hash`, `degraded` |
| `ActionCandidate` | One costed option | `action_type`, `feasible`, `infeasibility_reason`, `est_cost`, `est_delay_min`, `predicted_risk_after`, `predicted_risk_ci`, `ev_total`, `ev_breakdown`, `assumptions` |
| `Recommendation` | The chosen option plus its explanation | `selected_action_id`, `narrative`, `cited_evidence_ids`, `flip_sensitivity`, `grounding_check`, `prompt_version`, `model_id` |
| `Approval` | A human decision, bound to a world state | **`bound_context_hash`**, **`expires_at`**, `decision`, `decided_by`, `rationale` |
| `ActionExecution` | What actually happened | **`idempotency_key`** (unique), `status`, `attempts`, `result` |
| `OutcomeVerification` | Did it work? | `expected_effect`, `observed_effect`, `window_start`/`window_end`, `verdict`, `follow_up_required` |
| `AuditEvent` | Tamper-evident record | `seq`, `prev_hash`, `event_hash`, `actor`, `action`, `subject_ref`, `payload` |

### 2.3 Supporting entities

| Entity | Purpose |
|---|---|
| `Document` / `DocumentExtraction` | A parsed enterprise document and its typed fields, each with a page anchor for click-through provenance |
| `KnowledgeChunk` | SOP and manual chunks with `embedding vector(384)` and a full-text column, for hybrid retrieval |
| `ScenarioRun` | Ties an incident to IncidentForge ground truth (`scenario_id`, `pack_version`, `seed`) — without this, evaluation is guesswork |
| `ModelInvocation` | Per-call `model_id`, `prompt_version`, token counts, cache reads, cost estimate, latency — makes cost-per-incident a query |
| `BenchmarkRun` / `BenchmarkCaseResult` | Evaluation results with full provenance |
| `TelemetryEvent` | Time-series readings; Phase 1 loads from fixtures, Phase 2 from the event stream |

---

## 3. Design decisions worth explaining

### Why `baseline_probability` sits on every `RiskAssessment`

Because honest comparison should be structural, not optional. If the baseline
lives only in a training notebook, it quietly disappears from every
conversation about performance. Storing it beside the prediction means the UI,
the benchmark and the audit trail all show both numbers, permanently.

### Why `Recommendation` is separate from `ActionCandidate`

One incident produces several candidates and one recommendation. Collapsing
them loses the record of what was *considered and rejected* — which is exactly
what an auditor asks about, and exactly what makes the decision defensible.

### Why `Approval` carries a context hash

An approval granted at 14:02 against a 78% excursion probability is not an
approval of the same action at 14:20 when three new readings have arrived. The
hash covers the evidence IDs, their content hashes, the risk assessment and the
selected action. If any of that moved, the approval is stale and must be
re-sought. Without this, human approval is theatre: it approves a snapshot and
authorises an action against a different world.

### Why `correlation_key` and `superseded_by` exist

One degrading truck must produce one incident, not forty. New detections that
match an active incident's correlation key inside the suppression window attach
their evidence to it and are marked superseded. Absent this, the system is
unusable at fleet scale — a detail easy to miss until the first realistic demo.

### Why the audit chain is its own concern

`AuditEvent` rows are append-only: the application role is granted `INSERT` and
`SELECT` and denied `UPDATE` and `DELETE`, a trigger raises on either, and each
row hashes the previous one. Three independent layers, because any one of them
can be misconfigured. Even a compromised database yields a *detectably* broken
chain rather than a silently rewritten history.

### Why `entity_ref` rather than a foreign key to `Vehicle`

Incidents and evidence attach to several kinds of subject — a vehicle, a
shipment, a facility, a trailer. `entity_ref` is a typed `(kind, id)` pair. It
also crosses the store boundary cleanly: the referenced record usually lives in
SQL Server, where we cannot declare a foreign key even if we wanted one.

---

## 4. Identifiers and time

- **Identifiers.** Application entities use UUIDv7 — time-ordered, so index
  locality is good and creation order is recoverable without a separate column.
  Legacy identifiers keep their existing human-readable form (`AX-042`,
  `SH-2041`) because dispatchers speak them aloud.
- **Time.** Every timestamp is timezone-aware UTC. Naive datetimes are a lint
  error (`ruff` rule `DTZ`), because a mis-zoned `observed_at` would corrupt
  both freshness classification and conflict-window overlap.
- **Three distinct timestamps on evidence.** `observed_at` (when the world was
  in this state), `ingested_at` (when we learned it), and the
  `valid_from`/`valid_to` window. Collapsing these would make late-arriving
  telemetry indistinguishable from a current reading — which is precisely the
  bug that makes a stale-data exploit possible.
