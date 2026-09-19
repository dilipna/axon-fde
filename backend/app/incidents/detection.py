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
from datetime import datetime
from typing import Protocol

from backend.app.domain.enums import DetectedBy, IncidentSeverity
from backend.app.domain.evidence import Evidence

__all__ = [
    "BaselineDetector",
    "Detection",
    "Detector",
    "TemperatureEnvelope",
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
class TemperatureEnvelope:
    """The contractual limits a shipment must stay within.

    Resolved from evidence rather than passed around as configuration,
    because which envelope applies is itself contested: the ERP and the signed
    shipping document disagree for the flagship shipment, and the answer has
    to come from reconciliation rather than from whichever source was read
    last.
    """

    minimum_c: float
    maximum_c: float
    #: Which evidence this came from, so a detection can cite the envelope it
    #: was judged against and not merely the reading that breached it.
    source_evidence_ids: tuple[str, ...] = ()

    def breaches(self, temperature: float) -> bool:
        return temperature < self.minimum_c or temperature > self.maximum_c


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
