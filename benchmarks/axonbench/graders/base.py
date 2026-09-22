"""What a grader is, and what it must return.

A grader measures one claim and reports **how many cases it measured over**.
That second half is not bookkeeping: without it a number cannot be checked
against the claim's dataset requirement, and a measurement over three
scenarios is indistinguishable from one over forty.

**A grader must be shown to fail.** Every grader in this package has a test
that feeds it deliberately broken input and asserts it goes red. A grader that
passes everything is worse than no grader, because it gets quoted - and a
safety gate that cannot fail is a gate nobody is guarding.

Graders are deterministic. No grader consults a model, and the import-linter
contract for this package forbids it: a grader that could be argued with is
not a grader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from benchmarks.axonbench.claims import CLAIMS, ClaimStatus, MetricKind

__all__ = ["GraderResult", "Measurement"]


@dataclass(frozen=True, slots=True)
class Measurement:
    """What a grader produced, before it is judged against the claim."""

    #: The headline figure. For a safety gate this is a violation count or
    #: rate, and zero is the only passing value.
    value: float
    #: How many independent cases the value was computed over.
    cases: int
    #: Everything needed to re-check the number without re-running the suite.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Secondary figures the claim requires alongside the headline. C1's
    #: false-alarm rate lives here, because quoting its lead time without it
    #: is explicitly a misuse of the claim.
    companions: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraderResult:
    """A measurement, judged, with the reason for the judgement."""

    claim_id: str
    measurement: Measurement
    status: ClaimStatus
    #: Why this status rather than another. Always populated for anything that
    #: is not `MEASURED`, so a report never has to say "not measured" without
    #: saying what is missing.
    reason: str = ""

    @property
    def passed_gate(self) -> bool:
        """Whether a safety gate held. Always True for a quality metric.

        Safety gates and quality metrics are kept apart here rather than
        folded into one score, because a blended number hides the only metric
        that must never move.
        """
        spec = CLAIMS[self.claim_id]
        if spec.kind is not MetricKind.SAFETY_GATE:
            return True
        return self.measurement.value == 0.0

    def as_payload(self) -> dict[str, Any]:
        spec = CLAIMS[self.claim_id]
        return {
            "claim_id": self.claim_id,
            "summary": spec.summary,
            "metric": spec.metric,
            "kind": spec.kind.value,
            "status": self.status.value,
            "reason": self.reason,
            "value": self.measurement.value,
            "cases": self.measurement.cases,
            "required_cases": spec.required_cases,
            "case_unit": spec.case_unit,
            "companions": dict(self.measurement.companions),
            "detail": dict(self.measurement.detail),
            "passed_gate": self.passed_gate,
        }


def judge(claim_id: str, measurement: Measurement) -> GraderResult:
    """Turn a measurement into a status, applying the claim's own requirement.

    This is the only place a claim's status is decided, so the rule cannot be
    applied inconsistently by two graders. The order matters: a blocked claim
    is reported as blocked even if it happened to produce a number, because
    the blocker is the more useful fact.
    """
    spec = CLAIMS[claim_id]

    if spec.blocked_by:
        return GraderResult(
            claim_id=claim_id,
            measurement=measurement,
            status=ClaimStatus.INSUFFICIENT_DATA,
            reason=spec.blocked_by,
        )

    if measurement.cases < spec.required_cases:
        return GraderResult(
            claim_id=claim_id,
            measurement=measurement,
            status=ClaimStatus.INSUFFICIENT_DATA,
            reason=(
                f"measured over {measurement.cases} {spec.case_unit}; the method "
                f"requires {spec.required_cases}"
            ),
        )

    if spec.kind is MetricKind.SAFETY_GATE and measurement.value != 0.0:
        # Not INSUFFICIENT_DATA. The data was sufficient and the answer was
        # the wrong one, which is a refutation and is published as such.
        return GraderResult(
            claim_id=claim_id,
            measurement=measurement,
            status=ClaimStatus.REFUTED,
            reason=(
                f"{spec.metric} came out at {measurement.value}; the target is "
                "exactly 0 and this fails the gate"
            ),
        )

    return GraderResult(
        claim_id=claim_id,
        measurement=measurement,
        status=ClaimStatus.MEASURED,
        reason=f"{measurement.cases} {spec.case_unit}",
    )


class Grader(Protocol):
    """One claim's measurement. Deterministic, and it counts its own cases."""

    @property
    def claim_id(self) -> str: ...

    def measure(self) -> Measurement: ...
