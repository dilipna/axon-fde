# Verification release protocol v1

Frozen before verifier implementation, October 21, 2026, baseline `af2741f`.
Inputs: `data/scenarios/verification_release_v1.json`. Twenty-four constructed
cases, twelve per measurable effect. No statistical generalization claim.
Truth labels are explicitly authored physical outcomes; application code sees
only reported observations. Identical observations with opposite truths expose
the identifiability limit instead of pretending that variance proves health.

Policy declared before comparison:

- Measurable verification cannot finalize before its window closes. Reject
  the request without consuming the verdict or modifying lifecycle/audit state.
- Latest observation must be no more than two minutes before the deadline.
  The simulator emits each minute: this tolerates one missing scheduled sample.
  This is an explicit demo policy, not a field-calibrated logistics SLA.
- Recovery requires three unique observations spanning at least two minutes
  (the existing three samples at the simulator's one-minute cadence).
- An otherwise confirming trace with exactly one distinct value abstains.
  This is ambiguity, not a diagnosis of sensor failure. Nonconstant lies can
  still confirm; no independent instrument exists in this interface.
- Existing invalid/duplicate/conflict checks and slope count/coverage remain.
  Missing/ambiguous data requires follow-up and never resolves the incident.

Acceptance: all declared expected verdicts match; zero confirmations in the
target_fault subset; all fresh nonconstant honest controls retain their correct
verdicts. Honest constant and overly stale recoveries explicitly abstain and
count as missed recoveries. The two nonconstant lying controls intentionally
remain false confirmations. Report aggregate false confirmations with denominator
of true failures, missed recoveries with denominator of true successes, abstention
with denominator of all cases, and every case. Never market this as zero false
confirmations overall. No threshold adjustment after seeing the comparison.

Historical C11 is not rewritten. Any rerun over its existing scenarios is only
a regression diagnostic; its truth labels must be frozen from the baseline
grader before application changes, so the new verifier cannot redefine success
for its own evaluation. Real database tests must cover rejected early requests,
persisted abstention, follow-up, unresolved incidents and audit integrity, plus
valid recovery and warming controls. Full demo regression and UI/source release
qualification follow this protocol; passing the pack alone is not release closure.
