"""Detectors: turning evidence into "something is wrong".

Two detectors exist, and the difference between them is the whole measurement
this project rests on.

``BaselineDetector`` is what the customer has today: a threshold alarm. It
fires when the cargo temperature is already outside its contractual envelope.
By then the excursion has happened - the alarm is a notification, not a
warning. It is implemented properly rather than as a strawman, because a
lead-time claim measured against a deliberately weak baseline is worthless.

``PredictiveDetector`` is the comparison. It is *not* implemented here: it
needs the risk service, which is the next block. What this module fixes now is
the shape both must share, so the two are measured on identical inputs and
neither gets to see something the other does not.

**Detectors are pure.** They take evidence and return a finding. They do not
open incidents, write to the database or call anything. That is what lets the
same detector run over a live stream, over a replayed scenario, and inside a
benchmark grader without three implementations drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from backend.app.domain.enums import DetectedBy, IncidentSeverity
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.risk.features import DEFAULT_WINDOW_MINUTES

if TYPE_CHECKING:
    from backend.app.risk.service import RiskService

__all__ = [
    "CONSECUTIVE_READINGS_TO_FIRE",
    "BaselineDetector",
    "Detection",
    "Detector",
    "PredictiveDetector",
    "correlation_key",
]


def correlation_key(entity_kind: str, entity_id: str, incident_type: str) -> str:
    """The deduplication identity of a situation.

    One degrading truck is one incident, however many readings describe it, so
    the key deliberately excludes the reading, the timestamp and the severity.
    Including any of them would produce a new incident every time conditions
    worsened, which is the failure mode this exists to prevent.
    """
    return f"{entity_kind}:{entity_id}:{incident_type}"


@dataclass(frozen=True, slots=True)
class Detection:
    """A detector's finding about one entity at one moment."""

    incident_type: str
    entity_kind: str
    entity_id: str
    detected_by: DetectedBy
    detected_at: datetime
    severity: IncidentSeverity

    #: The evidence that justifies this finding. Carried so that an incident
    #: opens with its reasons attached rather than with a pointer to "whatever
    #: was in the store at the time", which is not reconstructible later.
    evidence_ids: tuple[str, ...]
    detail: str
    predicted_breach_at: datetime | None = None

    @property
    def correlation_key(self) -> str:
        return correlation_key(self.entity_kind, self.entity_id, self.incident_type)


class Detector(Protocol):
    """What every detector must look like.

    Both arms of the benchmark implement this, on the same inputs, so that a
    lead-time difference is attributable to the detection logic and not to one
    of them having been handed better data.
    """

    name: str

    def evaluate(
        self,
        evidence: list[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
    ) -> Detection | None: ...


class BaselineDetector:
    """A threshold alarm on the current cargo temperature.

    This is the incumbent system, implemented honestly. It fires the moment a
    reading falls outside the contractual envelope - which is to say, once the
    cargo is already out of spec and the damage, if any, has begun.

    Two details are deliberate rather than incidental:

    **It uses the most recent reading, not an average.** Smoothing would delay
    the alarm and flatter the comparison against the predictive detector. The
    baseline should be as good as a threshold alarm can be.

    **A single reading is enough.** Requiring consecutive breaches would cut
    false alarms and cost lead time. Real fleet alarms fire on one reading, and
    modelling something kinder than reality would overstate our advantage.
    """

    name = "baseline_threshold"

    def __init__(self, *, incident_type: str = "thermal_excursion") -> None:
        self._incident_type = incident_type

    def evaluate(
        self,
        evidence: list[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
    ) -> Detection | None:
        """Fire if the latest cargo temperature is outside the envelope."""
        readings = [
            item for item in evidence if item.observation_type == "cargo_temp_c" and item.is_active
        ]
        if not readings:
            return None

        latest = max(readings, key=lambda item: item.observed_at)
        temperature = float(latest.value)  # type: ignore[arg-type]
        if not envelope.breaches(temperature):
            return None

        return Detection(
            incident_type=self._incident_type,
            entity_kind=latest.entity_ref.kind,
            entity_id=latest.entity_ref.id,
            detected_by=DetectedBy.BASELINE,
            # The reading's own timestamp, not the wall clock. Lead time is
            # measured between detectors, and dating a detection to when the
            # replay happened to run would make that measurement meaningless.
            detected_at=latest.observed_at,
            severity=_severity_for(temperature, envelope),
            evidence_ids=(str(latest.id), *envelope.source_evidence_ids),
            detail=(
                f"cargo_temp_c {temperature:.2f} C is outside the permitted "
                f"{envelope.minimum_c:.1f} to {envelope.maximum_c:.1f} C envelope"
            ),
            # None, and that is the point: a threshold alarm has nothing to
            # say about the future. The breach is not predicted, it is past.
            predicted_breach_at=None,
        )


def _severity_for(temperature: float, envelope: TemperatureEnvelope) -> IncidentSeverity:
    """How far outside the envelope the reading is.

    The bands are in degrees over the limit rather than in percentages: a
    percentage of a limit that can be negative, as frozen cargo's is, produces
    nonsense. The thresholds are coarse on purpose - severity routes the
    incident to a role, and a finer scale would imply a precision the
    underlying sensor does not have.
    """
    excess = max(temperature - envelope.maximum_c, envelope.minimum_c - temperature)
    if excess >= 4.0:
        return IncidentSeverity.SEV1
    if excess >= 1.0:
        return IncidentSeverity.SEV2
    return IncidentSeverity.SEV3


#: Consecutive readings that must cross the threshold before an incident is
#: raised.
#:
#: Measured. On the flagship recording, with a 30-minute window:
#:
#:   1 reading   fires at minute 29  - inside the start-of-run pull-down, when
#:                                     the reefer is bringing the load down and
#:                                     nothing is wrong
#:   3 readings  fires at minute 102 - 16 min after saturation, 35 before breach
#:   5 readings  fires at minute 104
#:   10 readings fires at minute 109
#:
#: The single-reading case is the one that matters: a lone marginal estimate
#: flipped the alarm 57 minutes before the unit was in any trouble. Three is
#: the smallest number that survives the transient, and each further reading
#: costs lead time for nothing. Widening the window to 45 or 60 minutes fixes
#: the transient too, but fires at 103 - so the debounce is strictly better.
CONSECUTIVE_READINGS_TO_FIRE = 3


class PredictiveDetector:
    """Fires when the projected risk of leaving the envelope crosses threshold.

        The other arm of the measurement. It sees **the same evidence** as
        ``BaselineDetector`` - the replay hands both the identical window, and the
        baseline simply ignores everything but the latest reading. That is what
        makes a lead-time difference attributable to the logic rather than to one
        arm having been fed better data.

        **What the threshold means.** At the default horizon of 60 minutes,
        `SlopeExtrapolation` returns exactly 0.5 when the breach is projected to
        land on the horizon. So a threshold of 0.5 is not a tuned number: it is
        "the breach is projected to happen within the horizon". Moving it trades
        lead time against false alarms, and claim C1 is void unless both are
        quoted together.

    On the flagship recording this fires at **minute 102** - 16 minutes after
        the unit saturates at 86, 35 before the envelope is breached at 137. It
        does not fire at all on the false-alarm control. It *does* fire at minute
        59 on the sensor-drift scenario, where the instrument is lying and nothing
        is actually wrong; see `backend/app/risk/baselines.py` for why that is the
        expected Phase 1 result rather than a bug to tune away.
    """

    name = "predictive_risk"

    def __init__(
        self,
        service: RiskService,
        *,
        threshold: float = 0.5,
        horizon_minutes: int = 60,
        consecutive_readings: int = CONSECUTIVE_READINGS_TO_FIRE,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        incident_type: str = "thermal_excursion",
    ) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError(f"threshold={threshold} must lie strictly inside (0, 1)")
        if consecutive_readings < 1:
            raise ValueError("consecutive_readings must be at least 1")
        self._service = service
        self._threshold = threshold
        self._horizon_minutes = horizon_minutes
        self._consecutive = consecutive_readings
        self._incident_type = incident_type
        self._window_minutes = window_minutes

    @property
    def required_history_minutes(self) -> int:
        """How much history this detector needs handed to it.

        The fit window plus the debounce lookback. Getting this wrong is
        silent and total: with only the fit window available, the check for
        "did the threshold hold three readings ago?" finds too little data,
        answers no every time, and the detector never fires at all. Callers
        ask rather than assume.
        """
        return self._window_minutes + self._consecutive

    def _held(
        self, evidence: list[Evidence], *, envelope: TemperatureEnvelope, now: datetime
    ) -> bool:
        """Whether the threshold was crossed at each of the last N readings.

        Re-derived from the evidence rather than counted across calls, so the
        detector is a pure function of what it was handed. A counter would
        make the answer depend on call history, which is exactly the property
        a benchmark cannot have.
        """
        for offset in range(self._consecutive):
            moment = now - timedelta(minutes=offset)
            estimate = self._service.assess(
                evidence, envelope=envelope, now=moment, horizon_minutes=self._horizon_minutes
            )
            if estimate is None or estimate.probability < self._threshold:
                return False
        return True

    def evaluate(
        self,
        evidence: list[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
    ) -> Detection | None:
        estimate = self._service.assess(
            evidence,
            envelope=envelope,
            now=now,
            horizon_minutes=self._horizon_minutes,
        )
        # None means the trajectory could not be fitted. Silence, not zero.
        if estimate is None or estimate.probability < self._threshold:
            return None
        if not self._held(evidence, envelope=envelope, now=now):
            return None

        readings = [
            item for item in evidence if item.observation_type == "cargo_temp_c" and item.is_active
        ]
        if not readings:
            return None
        latest = max(readings, key=lambda item: item.observed_at)

        projected = estimate.minutes_to_breach
        predicted_breach_at = (
            latest.observed_at + timedelta(minutes=projected) if projected is not None else None
        )

        return Detection(
            incident_type=self._incident_type,
            entity_kind=latest.entity_ref.kind,
            entity_id=latest.entity_ref.id,
            detected_by=DetectedBy.AXON,
            detected_at=latest.observed_at,
            severity=_severity_for_risk(estimate.probability),
            evidence_ids=(str(latest.id), *envelope.source_evidence_ids),
            detail=(
                f"projected to leave the {envelope.minimum_c:.1f} to "
                f"{envelope.maximum_c:.1f} C envelope in {projected:.0f} min"
                if projected is not None
                else "projected to leave the envelope"
            ),
            # The whole point of this arm: it has something to say about the
            # future, and the baseline does not.
            predicted_breach_at=predicted_breach_at,
        )


def _severity_for_risk(probability: float) -> IncidentSeverity:
    """Severity from the predicted probability rather than from a reading.

    The predictive arm has no breach to measure the size of, so severity here
    reflects confidence that one is coming. Deliberately coarser than the
    baseline's degree bands: severity routes an incident to a role, and an
    uncalibrated probability cannot support a finer distinction.
    """
    if probability >= 0.9:
        return IncidentSeverity.SEV1
    if probability >= 0.7:
        return IncidentSeverity.SEV2
    return IncidentSeverity.SEV3
