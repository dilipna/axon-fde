# ADR-004 — Operational risk is computed outside the LLM

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE

## Context

The system must answer "how likely is this load to breach its temperature
ceiling in the next 30 minutes?" That number drives whether an incident is
raised, which interventions are considered, and — through the expected-value
layer — which intervention is recommended on a load worth up to $800k.

A language model can produce such a number. It can also produce a confident
explanation for it. Neither is evidence that the number is right.

Three properties are required and none of them are properties a language model
provides:

1. **Reproducibility.** The same inputs must yield the same probability, so an
   auditor can reconstruct a decision months later.
2. **Calibration.** The expected-value layer consumes the output *as a
   probability*. If "0.7" does not mean roughly 70%, every downstream cost
   comparison is wrong in a way that looks reasonable.
3. **Falsifiability.** The number must be comparable against a baseline on
   held-out data, so the claim register can state whether it works.

There is a fourth, softer reason. Alarm fatigue (customer brief §6.6) means the
operating threshold has to be chosen deliberately against a false-alarm budget.
That requires a score with a stable, measurable distribution.

## Decision

**All safety-critical probabilities are produced by a deterministic, versioned
ML service. The language model never computes, estimates, adjusts or restates a
risk probability as its own.**

Specifically:

- `RiskService.predict()` returns `{probability, model_version,
  feature_vector_hash, calibration_version, baseline_probability, degraded}`.
- The prediction enters the agent's context as **typed evidence** with
  `source = risk_model`, like any other observation.
- The model output always travels with its **non-ML baseline** so that honest
  comparison is structural rather than optional.
- The LLM may reference the number in its narrative, but the grounding check
  mechanically rejects any numeric claim that does not match an evidence value
  within tolerance. The model cannot silently round, restate or invent it.
- The same rule extends to **all** confidence scores: evidence confidence comes
  from the taxonomy formula, hypothesis confidence from a versioned scorer.
  (ADR-014 covers the general case.)

## Alternatives considered

### A. Ask the LLM for a probability

Zero infrastructure, immediately available, and handles novel situations
gracefully.

Rejected on all three required properties. Outputs are not reproducible across
runs or model versions; they are not calibrated (models are well known to
cluster on round numbers and to be sensitive to phrasing); and there is nothing
to compare against a baseline, so no claim about them could be supported.

### B. LLM produces a qualitative band, code maps it to a number

"High risk" → 0.8. Superficially safer.

Rejected because it launders an unfalsifiable judgement into a precise-looking
number. The mapping table would be arbitrary, the bands would still be
model-dependent, and the expected-value layer would consume a fabricated
probability while appearing rigorous. This is worse than option A because it
hides the problem.

### C. Deterministic rules only, no ML

Transparent and trivially reproducible.

Not rejected — **adopted as the mandatory baseline**, and shipped as the Phase 1
implementation. The ML model has to earn its place against it. If LightGBM does
not beat slope extrapolation by ≥5 points on lead-time-at-fixed-false-alarm-rate
after two feature iterations, the rule baseline *is* the production model and
the ML attempt is published as a negative result (see claim C2).

### D. LLM critiques or adjusts the ML output

Let the model flag when the prediction looks wrong.

Rejected for now. It reintroduces an unfalsifiable step into the safety path,
and the same benefit is available deterministically: feature-plausibility
checks, freshness gating, and the `degraded` flag already surface the cases
where the prediction should not be trusted.

## Consequences

### Accepted costs

- A training pipeline, a calibration step, a model registry and a feature-parity
  guarantee between training and serving all have to exist before the system
  can produce a number at all.
- The model is trained and evaluated on synthetic data from our own simulator.
  **This is circular and is stated as a limitation everywhere the metric
  appears.** Mitigations: splits by generative regime rather than by row,
  mandatory baselines, and held-out fault types and ambient ranges.
- The system cannot produce a risk estimate for a situation the feature builder
  does not cover. It degrades to the rule baseline and says so, rather than
  improvising — which is the correct behaviour but does mean visible gaps.

### Gained

- Any recommendation traces to an exact model artifact and feature vector.
- Calibration becomes measurable and displayable (reliability diagram in the
  Evaluations screen), which is what makes expected-value comparison defensible.
- The operating threshold can be chosen by expected cost rather than defaulting
  to 0.5, which is what keeps the false-alarm budget honest.
- The LLM's role narrows to what it is actually good at: proposing hypotheses,
  proposing actions, and explaining. That narrowing is testable via the
  `rules_only` ablation (C5).

## Enforcement

This decision is enforced mechanically, not by convention:

| Mechanism | What it prevents |
|---|---|
| `EvidenceSource` enum has no LLM member | Model output becoming an observation |
| Import-linter contract: decision engine forbids `anthropic` and `backend.app.llm` | Risk or EV logic reaching for a model |
| Grounding check compares narrative numbers against evidence values | The model restating a probability inaccurately |
| `test_llm_is_not_a_valid_evidence_source` | Someone adding an LLM source later without discussion |

## Revisit if

- Calibration of the deterministic model proves unachievable on the scenarios
  that matter, *and* a measured comparison shows a model-based estimate is
  better calibrated. That would be a surprising result and would need to be
  demonstrated on held-out regimes before this record is superseded.
