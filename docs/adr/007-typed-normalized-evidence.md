# ADR-007 — All evidence normalises to one typed, provenance-carrying model

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE

## Context

Evidence about a single shipment arrives in four incompatible shapes:

| Source | Native shape |
|---|---|
| ERP (SQL Server) | Relational rows |
| Telemetry | Time series |
| Bill of Lading | A scanned PDF |
| Reefer control panel | A photograph taken by the driver |

The customer's most expensive problem is that these **disagree** and nobody
notices (customer brief §6.3: the ERP says 2–10 °C, the signed BOL says 2–8 °C).

The naive approach is to render everything into text and give the model a large
prompt. That approach cannot detect the disagreement, because detecting it
requires knowing that "permitted maximum from the ERP" and "permitted maximum
from the BOL" are *the same quantity measured twice*. Text does not carry that
relationship. A model asked to notice it will sometimes notice and sometimes
not, and there is no way to measure which.

There is a second problem. Evidence has properties that matter for safety and
that free text discards: when it was true (as opposed to when we learned it),
how much the source is trusted, whether it has gone stale, and what artifact it
came from.

## Decision

**Every observation from every modality normalises into one `Evidence` record,
typed against a canonical observation taxonomy.**

The taxonomy (`data/taxonomy/observations.yaml`) declares, per observation
type: value shape, canonical unit, conflict tolerance, TTL, plausible range,
per-source reliability, and authority order.

Because the type is shared across modalities, **contradiction detection becomes
a deterministic value comparison**:

> Two active observations conflict iff they share an `observation_type` and an
> `entity_ref`, their validity windows overlap, and their values differ by more
> than the declared tolerance.

That is roughly 120 lines of pure, fully unit-testable code, and it replaces
what would otherwise be an unmeasurable model judgement.

### Load-bearing fields

| Field group | Why it exists |
|---|---|
| `observation_type`, `value`, `unit` | The typing that makes comparison possible. Adapters convert to canonical units *before* persisting. |
| `source`, `provenance`, `artifact_id` | Click-through to the exact query, page, or photograph behind a claim. |
| `observed_at`, `ingested_at`, `valid_from`/`valid_to` | Three distinct times. Collapsing them makes late-arriving telemetry indistinguishable from current data — the exact bug that enables a stale-data exploit. |
| `confidence`, `confidence_basis` | Computed, never asserted. See below. |
| `freshness` | Derived from age against TTL; gates how strongly the evidence may support a hypothesis. |
| `content_hash` | Powers approval-staleness binding (ADR-008). |
| `status` | `active` / `superseded` / `disputed` / `retracted`. |

### Two invariants

**1. Confidence is computed, never asserted.**

```
confidence = source_reliability × extraction_confidence × freshness_penalty
```

Every term is either declared in the taxonomy or is a per-field score from an
extraction model bounded to [0, 1]. No model-authored confidence reaches the
database. (ADR-014 generalises this.)

**2. A language model can never be the source of an observation.**

There is deliberately no `llm_inference` member of `EvidenceSource`. The LLM
links, interprets and narrates evidence; it never creates it. Enforced by the
enum, by the evidence service, and by `test_llm_is_not_a_valid_evidence_source`.

### `supersedes` is not `contradicts`

A newer reading from the *same* source replaces an older one. That is not a
disagreement between sources, and surfacing it to a dispatcher as a conflict
would generate noise that trains them to ignore real conflicts.

## Alternatives considered

### A. Text blobs into a large context window

Rejected. Cannot detect contradictions reliably, discards provenance and
timing, and makes groundedness unmeasurable. It is also the design that makes
the system a chatbot with extra steps.

### B. Per-modality schemas, compared pairwise by bespoke code

Rejected on combinatorics. Four modalities means six comparison paths today and
ten when a fifth arrives, each separately written and separately tested. One
normalised type means one comparison function.

### C. A knowledge graph with typed edges

Genuinely expressive, and rejected on cost. It adds a store and a query
language for expressiveness the problem does not need — the relationships here
are shallow (`supports`, `contradicts`, `supersedes`) and a join table in
PostgreSQL models them fully. Revisit only if multi-hop causal reasoning across
entities becomes a requirement.

### D. Let the model decide what conflicts

Rejected. This is the decision the ADR exists to prevent. It converts a
deterministic, testable, gradeable control into a nondeterministic one, and it
would make claim C6 unmeasurable.

## Consequences

### Accepted costs

- **Every new data source needs an adapter** that maps into the taxonomy,
  including unit conversion. That friction is deliberate: an unmapped source is
  a source whose conflicts are invisible.
- **The taxonomy must be maintained**, and a poorly chosen tolerance produces
  either false conflicts or missed ones. Mitigated by tolerances being declared
  in one reviewable file, and by conflict-detection accuracy being a graded
  benchmark metric (C6).
- **An observation that fits no declared type cannot be stored.** This is
  fatal by design — `UnknownObservationTypeError` — because a silent default
  would disable conflict detection for that value without anyone noticing.

### Gained

- Cross-modal contradiction detection with measurable precision and recall.
- Provenance for every claim, back to a page or a photograph.
- Freshness gating that makes the stale-evidence failure mode demonstrable.
- A clean substrate for the agent: a compact evidence table with IDs the model
  is required to cite, which is what makes the grounding check mechanical.
- Multimodal ablations (C4) become possible at all, because arms are defined by
  which sources contribute evidence.

## Implementation status

| Component | Phase |
|---|---|
| Taxonomy + loader + confidence formula | **Done** (Phase 0) |
| `Evidence` persistence, reconciliation engine, conflict detection | Phase 1 |
| Adapters: SQL, telemetry | Phase 1 |
| Adapter: structured document fixtures (the BOL conflict, no PDF parsing yet) | Phase 1 |
| Adapters: real VLM and PDF extraction | Phase 4 |

Building the conflict engine in Phase 1 against seeded structured fixtures, and
adding real extractors behind it in Phase 4, puts the hard part early and the
expensive part late.
