# ADR-008 — The shipped risk model is a logistic stack, not LightGBM

- **State:** Accepted
- **Date:** 2026-09-26

## Context

The plan named LightGBM + isotonic calibration and set a stop condition: if it
does not beat slope extrapolation by 5 points of lead time at a fixed
false-alarm rate after two feature iterations, ship the simpler model and
publish the negative result.

## Decision

Evaluated leave-one-regime-out with the three mandatory baselines. LightGBM
failed the stop condition after both iterations (-57 min, 95% CI over scenarios
[-74.5, -42.0]). A logistic regression over the same features plus the
baselines' scores generalised (AUC-PR 0.689 vs 0.284, ECE 0.030 vs 0.119).
It is stored as plain weights (`data/models/risk_v1/`) and needs no ML library
to serve.

## Consequences

- The lead-time advantage of the logistic model is **not established**
  (CI [-2.5, +25.0] min); only its ranking and calibration are.
- It was picked after the held-out regimes were consulted twice; confirm on
  fresh scenarios.
- The default detector is unchanged. Making the model primary changes C1 and
  the demo numbers and needs its own block, with C1 re-baselined.
- Twelve regimes are effectively sixty scenarios; more rows do not add
  independent evidence. More regimes and seeds would.
