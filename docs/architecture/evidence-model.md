# The evidence model

The architectural keystone. Everything upstream produces evidence; everything
downstream consumes it. The rationale for the design is in
[ADR-007](../adr/007-typed-normalized-evidence.md); this document is the
specification.

---

## 1. Schema

```python
class Evidence(BaseModel):
    id: UUID
    incident_id: UUID | None  # null = a fleet-wide standing observation
    entity_ref: EntityRef  # ("vehicle", "AX-042")

    # --- what kind of thing this is -----------------------------------
    source: EvidenceSource  # sql_legacy | telemetry | document_extraction
    # | visual_inspection | weather_api
    # | risk_model | sop_retrieval | human_input
    modality: Modality  # structured | timeseries | text | image

    # --- the observation itself ---------------------------------------
    observation_type: str  # a key in the canonical taxonomy
    value: ObservationValue  # numeric | categorical | boolean | set | interval
    unit: str | None  # canonical unit; adapters convert before persisting

    # --- time ----------------------------------------------------------
    observed_at: datetime  # when the world was in this state
    valid_from: datetime  # start of the validity window
    valid_to: datetime | None  # end; null = still valid
    ingested_at: datetime  # when we learned it

    # --- trust ---------------------------------------------------------
    confidence: float  # computed, never asserted
    confidence_basis: ConfidenceBasis
    freshness: FreshnessState  # fresh | aging | stale | not_applicable

    # --- provenance ------------------------------------------------------
    provenance: Provenance  # tool name + version, query/prompt hash,
    # page/bbox anchor, upstream evidence ids
    artifact_id: UUID | None  # the photograph or PDF this came from
    content_hash: str  # sha256 of the canonical value

    status: EvidenceStatus  # active | superseded | disputed | retracted
```

### Why three timestamps

| Field | Question it answers |
|---|---|
| `observed_at` | When was the world in this state? |
| `ingested_at` | When did we find out? |
| `valid_from` / `valid_to` | Over what interval does this hold? |

Collapsing these makes a telemetry reading that arrived five minutes late
indistinguishable from a current one. That is the precise bug that enables a
stale-evidence exploit, and it is why `observed_at` — not `ingested_at` —
drives both freshness and conflict-window overlap.

The validity window matters for a subtler reason: two readings taken an hour
apart are not in conflict, they are a trend. Only overlapping windows can
disagree.

---

## 2. Confidence

```
confidence = source_reliability[type][source]
           × extraction_confidence          # VLM / document only, else 1.0
           × freshness_penalty(age, ttl)    # 1.0 fresh → 0.6 aging → 0.3 stale
```

Every term is declared in `data/taxonomy/observations.yaml` except
`extraction_confidence`, which is a per-field score from an extraction model,
bounded to [0, 1] at the boundary.

**No language model ever supplies a confidence number that reaches the
database.** Implemented once in `Taxonomy.confidence()`; there is no second
code path.

A worked example, from the shipped taxonomy:

| Evidence | Reliability | Extraction | Freshness | Confidence |
|---|---|---|---|---|
| Telemetry `cargo_temp_c`, 30 s old | 0.95 | 1.0 | 1.0 fresh | **0.950** |
| Telemetry `cargo_temp_c`, 90 min old | 0.95 | 1.0 | 0.3 stale | **0.285** |
| Panel photo `cargo_temp_c`, just taken | 0.80 | 0.94 | 1.0 fresh | **0.752** |

The third row beating the second is the property that makes the stale-evidence
demo meaningful: a fresh photograph outranks a stale sensor, automatically.

---

## 3. Freshness

Per-type TTL from the taxonomy. Classification:

| State | Condition |
|---|---|
| `fresh` | `age ≤ 1 × ttl` |
| `aging` | `1 × ttl < age ≤ 3 × ttl` |
| `stale` | `age > 3 × ttl` |
| `not_applicable` | `ttl is null` — contractual facts do not age |

Freshness is a **gate**, not a label. Stale evidence cannot support a
hypothesis above `stale_evidence_confidence_cap` (0.45), which prevents a
confident diagnosis built on an hour-old reading. A negative age raises: a
reading from the future is clock skew, not a very fresh value.

---

## 4. Conflict detection

Two evidence records conflict iff **all four** hold:

1. same `observation_type`
2. same `entity_ref`
3. overlapping `[valid_from, valid_to)`
4. values differ beyond the declared `conflict_tolerance`

### Resolution

| Situation | Outcome |
|---|---|
| Same source, newer `observed_at` | `supersedes` — **not** a conflict |
| Different sources, beyond tolerance | `contradicts` — both stay `active`, incident flagged |
| Different sources, within tolerance | `corroborates` — independent agreement |

When a type declares an `authority_order`, the authoritative source's value is
**used**; the conflict remains **visible**. Authority decides which number
drives the decision, never whether the disagreement is mentioned.

An unresolved conflict on a `safety_critical` type blocks automatic incident
closure.

### The two flagship cases

**Spec mismatch.** ERP `permitted_temp_max_c = 10.0`, signed BOL `= 8.0`,
tolerance `0.0` → conflict. `authority_order` puts `document_extraction` first,
so 8.0 drives the decision while the disagreement stays on screen. This is
customer brief §6.3, detected automatically.

**Sensor drift.** Telemetry `cargo_temp_c = 5.4`, panel photograph `= 7.2`,
tolerance `0.5` → conflict. This raises a `sensor_fault` hypothesis that
telemetry alone cannot distinguish from a genuine excursion — and it is the
scenario family that lets the multimodal ablation (C4) succeed honestly.

---

## 5. How evidence reaches the agent

Never as raw blobs. The `ContextAssembler` builds a typed, budgeted
`ContextBundle`:

| Section | Contents |
|---|---|
| `incident_summary` | Typed incident header |
| `cargo_constraints` | Authoritative value **plus any conflicts, explicitly** |
| `telemetry_summary` | Latest value, 30-minute slope, variance, window min/max — not the raw series, which stays tool-fetchable |
| `evidence_table` | `(id, type, value, unit, source, age, confidence, status)` as a compact table |
| `conflicts` | An explicit list, never buried in prose |
| `risk_assessment` | Probability, **baseline**, horizon, model version |
| `sop_excerpts` | Retrieved passages with document and page citations |
| `prior_hypotheses` | On reopen, with the outcome that failed |
| `budget_metadata` | Token count, and what was dropped and why |

### Citation is mechanically enforced

Every evidence row carries its ID because the model is **required** to cite
IDs. The grounding check then verifies, deterministically:

- every cited `evidence_id` exists in the bundle,
- every numeric claim in the narrative matches an evidence value within tolerance.

This is what makes "unsupported claim rate" (C10) a measured number rather than
an LLM-judged one.

### Ordering is chosen for cache efficiency

The Anthropic API caches on an exact prefix match, and content renders in the
order `tools → system → messages`. The bundle therefore puts stable content
first — system prompt, tool schemas, SOP excerpts — and volatile content last —
evidence values, timestamps, risk numbers. A single changing byte early in the
prefix invalidates everything after it, so this ordering is load-bearing for
cost per incident (C12), not a stylistic choice.

`cache_read_input_tokens` is recorded on every invocation. If it sits at zero
across repeated incidents, a silent invalidator has crept into the prefix, and
that is alertable.

### Budgeting uses the provider's tokenizer

Token counts come from the Messages API `count_tokens` endpoint, never from a
third-party tokenizer, which would mis-budget against a different vocabulary.

When the bundle exceeds budget, sections are dropped by a **declared priority
order**, and what was dropped is recorded in `budget_metadata`. That record is
what makes context selection measurable: evidence recall against the
benchmark's `expected_evidence` becomes a graded metric rather than an
assumption.

---

## 6. Invariants

These are enforced in code and covered by tests, not merely documented.

| # | Invariant | Enforcement |
|---|---|---|
| 1 | A language model is never the `source` of evidence | No LLM member in `EvidenceSource`; checked by `test_llm_is_not_a_valid_evidence_source` |
| 2 | Confidence is computed, never supplied | Single implementation in `Taxonomy.confidence()` |
| 3 | An undeclared `observation_type` is fatal | `UnknownObservationTypeError` |
| 4 | Implausible values are rejected, not stored at low confidence | `ObservationSpec.is_plausible()` gate at ingestion |
| 5 | Values are canonical-unit before persistence | Adapter responsibility; unit asserted on write |
| 6 | Conflict detection is symmetric and irreflexive | Property tests |
| 7 | Evidence is immutable once written | Status transitions only; corrections supersede |
| 8 | A missing observation is represented as missing | Never fabricated; absence lowers confidence and is visible in the bundle |
