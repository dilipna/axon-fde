"""The feature builder. One implementation, shared by training and serving.

There is exactly one of these on purpose. A model trained on features built by
one code path and served features built by another will drift, and the drift
presents as a model that mysteriously performs worse in production than in the
notebook - expensive to debug, cheap to prevent.

**Nothing here can see ground truth.** The only input is ``Evidence``, and the
only way ground truth becomes evidence is if somebody adds a taxonomy type for
it. An import-linter contract additionally forbids ``backend`` from importing
``simulator``, where ``GroundTruthFrame`` lives. That is invariant I8 enforced
structurally rather than by inspection.

**Why a window and not a single reading.** A temperature on its own says
whether cargo is in spec now. Whether it is *going* to be out of spec is a
property of the trajectory, and one sample has no trajectory.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence

__all__ = [
    "DEFAULT_WINDOW_MINUTES",
    "MIN_READINGS_FOR_SLOPE",
    "MIN_WINDOW_COVERAGE",
    "RiskFeatures",
    "build_features",
    "least_squares_slope",
]

#: Minutes of history the trajectory is fitted over.
#:
#: Measured, not chosen. Against the flagship recording, where the unit
#: saturates at minute 86 and the envelope is breached at 137, with the
#: three-reading hold in `CONSECUTIVE_READINGS_TO_FIRE`:
#:
#:   window=30  first fires at minute 102  - 16 min after saturation, 35 before
#:   window=45  first fires at minute 105
#:   window=60  first fires at minute 105
#:
#: Thirty is the shortest window that survives the start-of-run pull-down
#: transient once the hold is applied, and it buys three more minutes of lead
#: time than the longer ones. The window and the hold were chosen together;
#: neither is defensible on its own, which is why both tables name the other.
DEFAULT_WINDOW_MINUTES = 30

#: Below this, a slope is not a trend. Two points fit a line perfectly, which
#: is the most confident possible way to be wrong.
MIN_READINGS_FOR_SLOPE = 5

#: How much of the window must actually be covered before a slope is reported,
#: as a fraction of ``window_minutes``.
#:
#: This constant exists because leaving it out was a bug. A minimum *count* of
#: readings looks like enough of a guard and is not: with one reading a minute,
#: five readings satisfy it four minutes into a run, and the estimator then
#: fits a "30-minute trend" to four minutes of pull-down transient. Measured
#: against the recordings, that fired at minute 4 on the flagship and minute 8
#: on the false-alarm control - both meaningless.
#:
#: The window length and the minimum sample count are therefore not
#: independent settings. A trend over N minutes requires N minutes of data,
#: and the count is only a floor beneath that.
MIN_WINDOW_COVERAGE = 0.95

#: Slopes below this are flat. Sensor quantisation alone produces gradients of
#: this order, and dividing headroom by one yields a time-to-breach in the
#: thousands of minutes: arithmetically fine, physically meaningless.
_FLAT_SLOPE_C_PER_MIN = 1e-4


def least_squares_slope(minutes: Sequence[float], values: Sequence[float]) -> float:
    """Ordinary least-squares gradient, in units per minute.

    Written out rather than pulled from numpy: it runs on every reading of
    every replay, and a five-line function with a known formula is easier to
    reason about than a dependency whose broadcasting rules must be recalled.

    Returns 0.0 for a degenerate window where every sample shares a timestamp,
    which is missing data rather than an infinitely steep trend.
    """
    count = len(minutes)
    if count < 2:
        return 0.0
    mean_x = sum(minutes) / count
    mean_y = sum(values) / count
    denominator = sum((x - mean_x) ** 2 for x in minutes)
    if denominator == 0.0:
        return 0.0
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(minutes, values, strict=True))
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class RiskFeatures:
    """Everything a risk estimator is allowed to see.

    A closed set, so "what did the model look at?" has an answer that fits on
    a screen and can be shown to somebody questioning a decision.
    """

    observed_at: datetime
    window_minutes: int
    readings_used: int

    cargo_temp_c: float
    envelope_min_c: float
    envelope_max_c: float
    #: Distance to the nearer limit. Negative once the envelope is breached.
    headroom_c: float
    temp_slope_c_per_min: float

    #: Projected minutes until the trajectory leaves the envelope. ``None``
    #: means "not heading out of spec", which is different from a very large
    #: number and must not be collapsed into one.
    minutes_to_breach: float | None

    ambient_temp_c: float | None
    compressor_rpm: float | None
    compressor_rpm_slope: float | None

    fault_codes: tuple[str, ...]
    has_prior_maintenance_warning: bool
    minutes_to_destination: float | None
    door_open: bool

    @property
    def in_breach(self) -> bool:
        return self.headroom_c <= 0.0

    @property
    def cooling_response(self) -> float | None:
        """Whether the unit is still answering a rising temperature.

        Negative when the compressor is winding down while cargo warms: a unit
        losing the fight rather than one working through a hot afternoon.
        ``None`` when the temperature is not rising, where the question does
        not arise.

        Measured at minute 100 over a 30-minute window:

            compressor_degradation  temp +0.016  rpm  -9.2   AL17 active
            sensor_drift            temp +0.039  rpm  +2.0   no fault code
            normal                  temp +0.002  rpm  +2.4   no fault code

        A compressor winding up while the reported temperature climbs fast is
        physically incoherent - the sensor is what is wrong. This separates
        the sensor-drift case **from telemetry alone**, which is why the Phase
        4 multimodal ablation has to control for it. See `claims.md`, C4.
        """
        if self.compressor_rpm_slope is None or self.temp_slope_c_per_min <= 0:
            return None
        return self.compressor_rpm_slope

    def vector(self) -> dict[str, Any]:
        """The ordered feature vector, for hashing and for model input.

        Explicit rather than derived from ``__slots__``: a comprehension would
        silently change the vector whenever a field was added, invalidating
        every stored ``feature_vector_hash`` without anyone noticing.
        """
        return {
            "cargo_temp_c": round(self.cargo_temp_c, 4),
            "envelope_min_c": self.envelope_min_c,
            "envelope_max_c": self.envelope_max_c,
            "headroom_c": round(self.headroom_c, 4),
            "temp_slope_c_per_min": round(self.temp_slope_c_per_min, 6),
            "minutes_to_breach": (
                None if self.minutes_to_breach is None else round(self.minutes_to_breach, 2)
            ),
            "ambient_temp_c": self.ambient_temp_c,
            "compressor_rpm": self.compressor_rpm,
            "compressor_rpm_slope": (
                None if self.compressor_rpm_slope is None else round(self.compressor_rpm_slope, 4)
            ),
            "fault_code_count": len(self.fault_codes),
            "fault_codes": list(self.fault_codes),
            "has_prior_maintenance_warning": self.has_prior_maintenance_warning,
            "minutes_to_destination": self.minutes_to_destination,
            "door_open": self.door_open,
            "window_minutes": self.window_minutes,
            "readings_used": self.readings_used,
        }

    def digest(self) -> str:
        """A stable hash of the feature vector.

        Stored on every ``RiskAssessment`` so a prediction can be tied to the
        exact inputs that produced it. Without it, reproducing a past
        assessment means trusting that the feature code has not changed since,
        which over a project's lifetime it certainly has.
        """
        canonical = json.dumps(self.vector(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _latest(
    evidence: Sequence[Evidence], observation_type: str, *, until: datetime | None = None
) -> Evidence | None:
    candidates = [
        item
        for item in evidence
        if item.observation_type == observation_type
        and item.is_active
        and (until is None or item.observed_at <= until)
    ]
    return max(candidates, key=lambda item: item.observed_at) if candidates else None


def _series(
    evidence: Sequence[Evidence],
    observation_type: str,
    *,
    since: datetime,
    until: datetime,
) -> tuple[list[float], list[float]]:
    """Minute offsets and values for one observation type inside the window.

    Bounded at both ends. The upper bound matters more than it looks: asking
    what the features were at an earlier moment is how the detector checks
    that a threshold crossing has held, and without it that question would be
    answered using readings from after the moment being asked about.
    """
    points = sorted(
        (
            item
            for item in evidence
            if item.observation_type == observation_type
            and item.is_active
            and since <= item.observed_at <= until
        ),
        key=lambda item: item.observed_at,
    )
    if not points:
        return [], []
    origin = points[0].observed_at
    minutes = [(item.observed_at - origin).total_seconds() / 60.0 for item in points]
    values = [float(item.value) for item in points]  # type: ignore[arg-type]
    return minutes, values


def _project_minutes_to_breach(
    *, current: float, slope: float, envelope: TemperatureEnvelope
) -> float | None:
    """When the trajectory is projected to leave the envelope.

    Handles both directions. Frozen cargo breaches by warming; a reefer stuck
    on breaches by freezing pharmaceuticals that must stay above 2 C. A
    projection that only looked upward would be silent on the second, which is
    the one that destroys a vaccine shipment.
    """
    if envelope.breaches(current):
        return 0.0
    if abs(slope) < _FLAT_SLOPE_C_PER_MIN:
        return None
    limit = envelope.maximum_c if slope > 0 else envelope.minimum_c
    return (limit - current) / slope


def build_features(
    evidence: Sequence[Evidence],
    *,
    envelope: TemperatureEnvelope,
    now: datetime,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    context: Sequence[Evidence] = (),
) -> RiskFeatures | None:
    """Build the feature vector from a window of evidence.

    Returns ``None`` when there is not enough history to fit a trajectory.
    That is invariant I6: a risk score computed from two readings is a number
    the system has no business producing, and producing it anyway is worse
    than saying nothing, because a confident wrong answer gets acted upon.

    ``context`` carries slow-moving evidence - the maintenance history, the
    contractual envelope - which sits outside the rolling window and would
    otherwise age out of it.
    """
    since = now - timedelta(minutes=window_minutes)
    minutes, temperatures = _series(evidence, "cargo_temp_c", since=since, until=now)
    if len(temperatures) < MIN_READINGS_FOR_SLOPE:
        return None
    # Both guards are needed, and the second is the one that is easy to
    # forget: enough readings, *and* enough elapsed time for them to describe
    # a trend over the window that was asked for.
    if (minutes[-1] - minutes[0]) < window_minutes * MIN_WINDOW_COVERAGE:
        return None

    current = temperatures[-1]
    slope = least_squares_slope(minutes, temperatures)

    rpm_minutes, rpm_values = _series(evidence, "compressor_rpm", since=since, until=now)
    rpm_slope = (
        least_squares_slope(rpm_minutes, rpm_values)
        if len(rpm_values) >= MIN_READINGS_FOR_SLOPE
        else None
    )

    ambient = _latest(evidence, "ambient_temp_c", until=now)
    rpm = _latest(evidence, "compressor_rpm", until=now)
    faults = _latest(evidence, "fault_code", until=now)
    remaining = _latest(evidence, "minutes_to_destination", until=now)
    door = _latest(evidence, "door_state", until=now)
    warning = _latest([*evidence, *context], "maintenance_warning", until=now)

    return RiskFeatures(
        observed_at=now,
        window_minutes=window_minutes,
        readings_used=len(temperatures),
        cargo_temp_c=current,
        envelope_min_c=envelope.minimum_c,
        envelope_max_c=envelope.maximum_c,
        headroom_c=min(envelope.maximum_c - current, current - envelope.minimum_c),
        temp_slope_c_per_min=slope,
        minutes_to_breach=_project_minutes_to_breach(
            current=current, slope=slope, envelope=envelope
        ),
        ambient_temp_c=None if ambient is None else float(ambient.value),  # type: ignore[arg-type]
        compressor_rpm=None if rpm is None else float(rpm.value),  # type: ignore[arg-type]
        compressor_rpm_slope=rpm_slope,
        fault_codes=tuple(faults.value) if faults is not None else (),  # type: ignore[arg-type]
        has_prior_maintenance_warning=bool(warning is not None and warning.value),
        minutes_to_destination=(
            None if remaining is None else float(remaining.value)  # type: ignore[arg-type]
        ),
        door_open=bool(door is not None and door.value in ("open", "ajar")),
    )
