# ADR-001 — Solve proactive incident response, not conversational retrieval

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE, VP Operations (sponsor)

## Context

Axon's stated request was:

> "We want an AI assistant that lets dispatchers ask questions about our fleet."

Discovery (see [`customer-brief.md`](../customer-brief.md)) found that
retrieval is not the constraint. Dispatchers spend 11–24 minutes per incident,
almost all of it assembling evidence from five systems, and they begin that
work only *after* a threshold alarm has fired — which for a thermal system is
after the cargo is already out of specification.

The decisive observation was a pharma load reading 5.2 → 6.9 °C over 50 minutes
against an 8 °C ceiling. Every individual reading was in spec. The dashboard was
green throughout. The trend was obvious and nothing in the stack was watching
it.

A conversational interface over the same five systems would compress the
assembly step. It would not move the moment of detection, would not reconcile
sources that disagree, would not compare interventions, and would not record
whether anything worked.

## Decision

**AxonFDE is an incident-response system that predicts, investigates,
recommends, gates, executes and verifies. It is not a question-answering
interface over enterprise data.**

The natural-language query capability the customer originally asked for is
still built, because dispatchers genuinely ask ad-hoc questions — but it is a
**sidebar**, deliberately outside the incident workflow, and it is not the
product.

The product loop is:

```
understand → predict → investigate → decide → approve → act → verify → learn
```

## Alternatives considered

### A. Build what was asked: RAG chatbot over the five systems

Fastest to deliver, immediately legible to the customer, and low technical risk.

Rejected because it optimises the wrong step. It leaves detection lagging,
leaves contradictions undetected, leaves decisions unrecorded, and leaves
outcomes unverified. It would be judged a success at demo time and quietly
abandoned within a quarter, because it would not change any number the VP
Operations is measured on.

### B. Predictive alerting only (no investigation or action)

Ship the risk model and route alerts into the existing workflow.

Rejected on the alarm-fatigue finding. Dispatchers already mute alert
categories. Adding a new predictive alert stream without also collapsing the
11–24 minute assembly cost would increase workload at the moment of highest
pressure, and the alerts would be muted like the others.

### C. Full autonomous remediation

Let the system reroute trucks and notify customers directly.

Rejected on risk and on customer appetite. A wrong autonomous diversion on a
$250k pharma load is a worse outcome than a slow human decision, and the
Compliance Lead's audit requirement presumes a named human decision-maker.
Autonomy also removes the approval record that makes O2 (the compliance
product) valuable.

## Consequences

### Accepted costs

- **Much larger scope than a chatbot.** The loop requires an evidence model, a
  risk service, a policy engine, an approval mechanism, an action layer and a
  verification workflow before it is coherent at all. There is no useful
  fraction of this loop — a system that recommends but cannot verify is not a
  smaller version of the product, it is a different and weaker one.
- **The demo is harder to explain in 30 seconds** than a chat box. Mitigated by
  the Legacy Mode toggle, which shows the same data as the current workflow and
  makes the difference visceral.
- **Every claim now requires a baseline.** "We detect earlier" is meaningless
  without a threshold detector running on the identical stream, so that
  detector becomes a required component rather than an optional comparison.

### Gained

- Lead time becomes the product's measurable North Star (C1).
- The audit trail becomes a compliance capability (O2) rather than logging.
- Contradiction detection becomes possible at all, because evidence is typed.
- The system can be evaluated objectively against ground truth, which a
  retrieval chatbot largely cannot.

### Implications for later decisions

- Detection must be continuous, which eventually justifies streaming (ADR-009).
- Recommendations must be gated, which requires a policy engine outside the
  model (ADR-004 and ADR-008).
- "Earlier than a threshold" must be measured, which forces evaluation into
  Phase 1 (ADR-012).

## Revisit if

- Measurement shows the predictive path provides no usable lead time at an
  acceptable false-alarm rate (C1 and C2 both refuted). In that case the honest
  product *is* closer to option A, and this record should be superseded rather
  than quietly ignored.
