"""Measuring lead time between the two detection arms.

Claim C1 is "AxonFDE raises an actionable incident before a threshold alarm
fires", and this is the arithmetic behind it. It is deliberately a separate,
pure function rather than a method on anything: the number has to be
computable from two stored incidents long after the run, by a benchmark, by a
report, and by somebody checking the claim.

**Lead time alone is meaningless and this module says so.** A detector that
alerts constantly has infinite lead time and no value. ``LeadTime`` therefore
carries ``alarm_was_correct`` - whether a breach actually occurred - so that a
false alarm cannot be quietly counted as a large positive lead time. Claim C1
requires the false-alarm rate to be quoted alongside, and the shape of this
type is what makes forgetting awkward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = ["LeadTime", "measure_lead_time"]


@dataclass(frozen=True, slots=True)
class LeadTime:
    """How much earlier the predictive arm fired than the threshold alarm."""

    #: Positive when the predictive arm fired first, which is the point.
    #: Negative would mean it fired *after* the threshold alarm, which is a
    #: result worth seeing rather than clamping to zero.
    minutes: float | None

    axon_detected_at: datetime | None
    baseline_detected_at: datetime | None

    #: False when the predictive arm fired and no breach followed. Without
    #: this, a scenario where nothing was wrong contributes a large positive
    #: lead time and inflates the headline.
    alarm_was_correct: bool

    @property
    def is_measurable(self) -> bool:
        """Whether both arms fired, which is the only case C1 is computed over."""
        return self.minutes is not None

    def describe(self) -> str:
        if not self.is_measurable:
            if self.axon_detected_at is None and self.baseline_detected_at is None:
                return "neither arm fired"
            if self.axon_detected_at is None:
                return "only the threshold alarm fired: no lead time, a missed detection"
            return "only the predictive arm fired: no threshold alarm to measure against"
        assert self.minutes is not None
        verdict = "" if self.alarm_was_correct else " (FALSE ALARM - excluded from C1)"
        return f"{self.minutes:.0f} min of lead time{verdict}"


def measure_lead_time(
    *,
    axon_detected_at: datetime | None,
    baseline_detected_at: datetime | None,
    breach_occurred: bool,
) -> LeadTime:
    """Compute the gap between the two arms.

    ``breach_occurred`` comes from the scenario's declared ground truth, which
    is why this lives outside the application's decision path: the application
    must never see it (invariant I8). A benchmark may.
    """
    measurable = axon_detected_at is not None and baseline_detected_at is not None
    minutes = (
        (baseline_detected_at - axon_detected_at).total_seconds() / 60.0  # type: ignore[operator]
        if measurable
        else None
    )
    return LeadTime(
        minutes=minutes,
        axon_detected_at=axon_detected_at,
        baseline_detected_at=baseline_detected_at,
        # An alarm is correct only if something was actually going wrong. A
        # predictive arm that fired on a scenario with no breach is a false
        # alarm however early it was.
        alarm_was_correct=breach_occurred,
    )
