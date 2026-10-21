"""Grading an action against the claim it made.

Axon's Compliance Lead described the gap this closes: after a reroute, nobody
records whether it worked. The system acts, the incident is marked handled,
and the question "did that help?" has no answer anywhere. This module is the
answer, and it is deterministic - no model grades an outcome, because a
grader that can be talked into a verdict is not a grader.

**Four verdicts, and the two boring ones matter most.**

``INCONCLUSIVE`` exists because of invariant I6: absence is represented as
absence. A verification window with no readings in it is not a pass. Treating
missing data as success is how a fleet with a dead telemetry link reports a
hundred percent intervention success rate.

``NOT_APPLICABLE`` exists for the same reason from the other side. Flagging a
vehicle for inspection changes a record, not a temperature; there is nothing
for a sensor to confirm. Scoring it as a pass would pad every outcome metric
with actions nobody could check, and scoring it as a failure would punish an
action that did exactly what was asked.

Neither resolves an incident. A verdict that cannot be established leaves the
incident where it is and asks for a human, which is the honest move and also
the one that keeps the resolution rate a measure of outcomes rather than of
optimism.

Pure: readings in, verdict out. No database, no clock, no model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from backend.app.actions.effects import EffectKind, ExpectedEffect
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.risk.features import (
    MIN_READINGS_FOR_SLOPE,
    MIN_WINDOW_COVERAGE,
    least_squares_slope,
)

__all__ = [
    "RECOVERY_HOLD_READINGS",
    "STOPPED_RISING_SLOPE_C_PER_MIN",
    "Verdict",
    "VerificationResult",
    "evaluate_effect",
]

#: Above this gradient the cargo is still warming, in degrees Celsius per
#: minute.
#:
#: **Measured against both recordings** with a rolling 90-minute least-squares
#: fit. The healthy control run never exceeds 0.0080 C/min over that window;
#: the degrading compressor never falls below 0.0153. Ten thousandths sits
#: roughly in the middle with about a 25% margin on each side, so neither a
#: noisy healthy run nor a slow failure lands on the boundary.
#:
#: It is emphatically **not** "any positive slope". Sensor quantisation and
#: ordinary ambient variation put a healthy trailer at a few thousandths, and
#: a threshold at zero would fail every intervention that worked.
STOPPED_RISING_SLOPE_C_PER_MIN = 0.010

#: How many consecutive readings must be back inside the envelope before a
#: recovery is called.
#:
#: Three, not one. A single reading dipping into spec is noise - the same
#: lesson the predictive detector learned, where a one-reading trigger fired
#: on transients and the corrected answer needed a hold. One reading in spec
#: after a reroute is a thermometer twitching; three in a row is a trend.
RECOVERY_HOLD_READINGS = 3

# Release protocol v1: one-minute demo telemetry, allowing one missing sample.
# Operational policy, not a field-calibrated sensor health threshold.
MAX_FINAL_READING_AGE_MINUTES = 2.0
MIN_RECOVERY_HOLD_MINUTES = 2.0


class Verdict(StrEnum):
    """What the data says about the claim."""

    CONFIRMED = "confirmed"
    FAILED = "failed"
    #: The data could not settle it. Not a pass.
    INCONCLUSIVE = "inconclusive"
    #: There was never anything to measure.
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The verdict, why, and the numbers behind it."""

    verdict: Verdict
    detail: str
    #: Stored as ``outcome_verification.observed_effect``. Holds the actual
    #: measurement so a verdict can be re-checked without the raw readings,
    #: and so a disputed one can be argued from numbers.
    observed: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_follow_up(self) -> bool:
        """Whether a human still has to look at this.

        True for everything except a confirmed recovery. An inconclusive
        verification needs someone to find out why the data is missing, and an
        unobservable action needs someone to close the incident on judgement -
        neither is a state the system should quietly leave behind.
        """
        return self.verdict is not Verdict.CONFIRMED


def evaluate_effect(
    effect: ExpectedEffect,
    *,
    readings: Sequence[tuple[datetime, float]],
    envelope: TemperatureEnvelope,
    window_start: datetime,
    window_end: datetime,
) -> VerificationResult:
    """Grade one expected effect against the readings that followed it.

    ``readings`` are ``(observed_at, cargo_temp_c)`` pairs in any order; they
    are filtered to the window and sorted here, so a caller cannot change the
    verdict by changing the order it read the evidence in.
    """
    if effect.kind is EffectKind.NONE_OBSERVABLE:
        return VerificationResult(
            verdict=Verdict.NOT_APPLICABLE,
            detail="the action changes a record, not a temperature; nothing to measure",
        )

    if (
        window_start.utcoffset() is None
        or window_end.utcoffset() is None
        or window_end <= window_start
    ):
        return VerificationResult(
            Verdict.INCONCLUSIVE,
            "verification needs an ordered, timezone-aware window",
            {"invalid_context": "window"},
        )
    if (
        not isfinite(envelope.minimum_c)
        or not isfinite(envelope.maximum_c)
        or envelope.minimum_c > envelope.maximum_c
    ):
        return VerificationResult(
            Verdict.INCONCLUSIVE,
            "verification needs finite, ordered temperature limits",
            {"invalid_context": "envelope"},
        )

    # Repeated delivery is not an independent measurement. This interface has
    # no sensor identity, so disagreement at one instant cannot be reconciled
    # here. Do not select whichever source happened to arrive last.
    by_time: dict[datetime, float] = {}
    invalid_count = 0
    duplicate_count = 0
    conflicts: set[datetime] = set()
    for moment, value in readings:
        if moment.utcoffset() is None:
            return VerificationResult(
                Verdict.INCONCLUSIVE,
                "a reading has no timezone; its verification window is unknown",
                {"invalid_context": "reading_timestamp"},
            )
        if not window_start <= moment <= window_end:
            continue
        if isinstance(value, bool) or not isfinite(value):
            invalid_count += 1
            continue
        if moment in by_time:
            if by_time[moment] != value:
                conflicts.add(moment)
            else:
                duplicate_count += 1
        else:
            by_time[moment] = value

    if invalid_count or conflicts:
        return VerificationResult(
            Verdict.INCONCLUSIVE,
            "invalid or conflicting temperature evidence requires review",
            {
                "invalid_reading_count": invalid_count,
                "conflicting_timestamp_count": len(conflicts),
            },
        )
    inside = sorted(by_time.items())
    if not inside:
        return VerificationResult(
            verdict=Verdict.INCONCLUSIVE,
            detail=(
                f"no cargo temperature readings between {window_start.isoformat()} and "
                f"{window_end.isoformat()}; absence of data is not evidence of recovery"
            ),
            observed={"reading_count": 0},
        )

    if effect.kind is EffectKind.TEMPERATURE_WITHIN_ENVELOPE:
        result = _grade_recovery(inside, envelope)
    else:
        result = _grade_slope(inside, window_start, window_end, effect)
    if result.verdict in {Verdict.CONFIRMED, Verdict.FAILED}:
        age = (window_end - inside[-1][0]).total_seconds() / 60.0
        result.observed["final_reading_age_minutes"] = age
        if age > MAX_FINAL_READING_AGE_MINUTES:
            result = VerificationResult(
                Verdict.INCONCLUSIVE,
                "readings are stale at the verification deadline; current outcome is unknown",
                result.observed | {"abstention_reason": "stale_at_deadline"},
            )
    if result.verdict is Verdict.CONFIRMED and len({v for _, v in inside}) == 1:
        result = VerificationResult(
            Verdict.INCONCLUSIVE,
            "constant single-channel readings cannot distinguish steady recovery "
            "from a stuck sensor",
            result.observed | {"abstention_reason": "constant_trace_ambiguity"},
        )
    if duplicate_count:
        result.observed["duplicate_reading_count"] = duplicate_count
    return result


def _grade_recovery(
    readings: list[tuple[datetime, float]], envelope: TemperatureEnvelope
) -> VerificationResult:
    """Is the cargo back in spec, and has it stayed there?"""
    if len(readings) < RECOVERY_HOLD_READINGS:
        return VerificationResult(
            verdict=Verdict.INCONCLUSIVE,
            detail=(
                f"{len(readings)} readings in the window, fewer than the "
                f"{RECOVERY_HOLD_READINGS} needed to tell a recovery from a "
                "single reading twitching into spec"
            ),
            observed={"reading_count": len(readings)},
        )

    tail = readings[-RECOVERY_HOLD_READINGS:]
    hold_minutes = (tail[-1][0] - tail[0][0]).total_seconds() / 60.0
    if hold_minutes < MIN_RECOVERY_HOLD_MINUTES:
        return VerificationResult(
            Verdict.INCONCLUSIVE,
            "recovery samples are too close together to establish the required hold",
            {
                "reading_count": len(readings),
                "hold_minutes": hold_minutes,
                "abstention_reason": "compressed_recovery_hold",
            },
        )
    breaching = [value for _, value in tail if envelope.breaches(value)]
    observed = {
        "reading_count": len(readings),
        "final_temperatures_c": [round(value, 3) for _, value in tail],
        "envelope_c": [envelope.minimum_c, envelope.maximum_c],
    }
    if breaching:
        return VerificationResult(
            verdict=Verdict.FAILED,
            detail=(
                f"{len(breaching)} of the last {RECOVERY_HOLD_READINGS} readings are outside "
                f"[{envelope.minimum_c}, {envelope.maximum_c}] C"
            ),
            observed=observed,
        )
    return VerificationResult(
        verdict=Verdict.CONFIRMED,
        detail=(
            f"the last {RECOVERY_HOLD_READINGS} readings are inside "
            f"[{envelope.minimum_c}, {envelope.maximum_c}] C"
        ),
        observed=observed,
    )


def _grade_slope(
    readings: list[tuple[datetime, float]],
    window_start: datetime,
    window_end: datetime,
    effect: ExpectedEffect,
) -> VerificationResult:
    """Has the rise stopped?

    Guarded by both a reading count and a coverage fraction, and it needs to
    be both. A count alone is the bug documented in ``risk.features``: five
    readings satisfy a minimum four minutes into a ninety-minute window, and
    the fit then describes four minutes of whatever was happening at the
    start. A ninety-minute claim requires ninety minutes of data.
    """
    minutes = [(moment - window_start).total_seconds() / 60.0 for moment, _ in readings]
    values = [value for _, value in readings]
    span = minutes[-1] - minutes[0]
    required_span = effect.within_minutes * MIN_WINDOW_COVERAGE

    observed: dict[str, Any] = {
        "reading_count": len(readings),
        "span_minutes": round(span, 2),
        "required_span_minutes": round(required_span, 2),
    }

    if len(readings) < MIN_READINGS_FOR_SLOPE or span < required_span:
        return VerificationResult(
            verdict=Verdict.INCONCLUSIVE,
            detail=(
                f"{len(readings)} readings spanning {span:.1f} of the "
                f"{effect.within_minutes} minute window; a trend over that window "
                f"needs at least {required_span:.1f} minutes of it covered"
            ),
            observed=observed,
        )

    slope = least_squares_slope(minutes, values)
    if not isfinite(slope):
        return VerificationResult(
            Verdict.INCONCLUSIVE,
            "the fitted slope is not finite; numerical failure is not recovery",
            observed=observed,
        )
    observed["slope_c_per_min"] = round(slope, 6)
    observed["threshold_c_per_min"] = STOPPED_RISING_SLOPE_C_PER_MIN

    if slope > STOPPED_RISING_SLOPE_C_PER_MIN:
        return VerificationResult(
            verdict=Verdict.FAILED,
            detail=(
                f"cargo temperature is still rising at {slope:.4f} C/min, above the "
                f"{STOPPED_RISING_SLOPE_C_PER_MIN} C/min a healthy trailer stays under"
            ),
            observed=observed,
        )
    return VerificationResult(
        verdict=Verdict.CONFIRMED,
        detail=(
            f"cargo temperature gradient is {slope:.4f} C/min, at or below the "
            f"{STOPPED_RISING_SLOPE_C_PER_MIN} C/min threshold"
        ),
        observed=observed,
    )
