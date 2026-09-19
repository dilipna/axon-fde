"""The incident state machine, and what happens when a detection arrives.

Two separable things live here.

``TRANSITIONS`` is a pure table of which status may follow which. It is a
table rather than a scattering of ``if`` statements because the legal moves
are a property of the domain that an auditor may reasonably ask to see, and
because a state machine expressed as control flow is one nobody can enumerate.

``IncidentService`` applies a detection to the store: attach to a live
incident, or open a new one. That decision is deduplication, and getting it
wrong is expensive in both directions. Too eager and one degrading truck
produces forty incidents and the dispatcher stops reading them. Too lax and a
genuinely separate excursion is swallowed by an unrelated open incident and
nobody is told.

**Why a suppression window and not just "is anything open".** An incident that
has been open for six hours on a vehicle whose new detection looks the same is
almost certainly the same situation. One that closed two days ago is not, even
though its correlation key is identical. The window is what separates the two,
and it is a parameter because the right value differs by incident type.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.app.audit.service import AuditService
from backend.app.db.app.models import Incident
from backend.app.db.app.repositories.evidence import EvidenceRepository
from backend.app.db.app.repositories.incident import IncidentRepository
from backend.app.domain.enums import IncidentStatus
from backend.app.domain.evidence import Evidence
from backend.app.incidents.detection import Detection

__all__ = [
    "DEFAULT_SUPPRESSION_WINDOW",
    "TRANSITIONS",
    "IllegalTransitionError",
    "IncidentOutcome",
    "IncidentService",
    "is_legal_transition",
]

#: How long after an incident was detected a matching detection is treated as
#: the same situation. Six hours is longer than any single scenario run and
#: shorter than a working day, so a genuinely new excursion on the next shift
#: opens its own incident.
DEFAULT_SUPPRESSION_WINDOW = timedelta(hours=6)

#: The legal moves. Absence from this table is a bug, not a silent no-op.
#:
#: `RESOLVED` can return to `VERIFYING`: outcome verification runs on a
#: schedule after the action, and a verification that fails must be able to
#: reopen what it just closed. An incident that could never reopen would make
#: the resolution rate a measure of optimism rather than of outcomes.
TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.DETECTED: frozenset(
        {
            IncidentStatus.INVESTIGATING,
            IncidentStatus.SUPERSEDED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.INVESTIGATING: frozenset(
        {
            IncidentStatus.AWAITING_APPROVAL,
            # Investigation can conclude that nothing needs doing. That path
            # has to exist, or the only way to close an incident is to act on
            # it, and the system acquires a bias toward action.
            IncidentStatus.RESOLVED,
            IncidentStatus.SUPERSEDED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.AWAITING_APPROVAL: frozenset(
        {
            IncidentStatus.ACTING,
            # A denied or expired approval returns here rather than closing:
            # the situation has not changed just because the answer was no.
            IncidentStatus.INVESTIGATING,
            IncidentStatus.SUPERSEDED,
            IncidentStatus.CLOSED,
        }
    ),
    IncidentStatus.ACTING: frozenset(
        {
            IncidentStatus.VERIFYING,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.SUPERSEDED,
        }
    ),
    IncidentStatus.VERIFYING: frozenset(
        {
            IncidentStatus.RESOLVED,
            # Verification failed: the intervention did not work, so the
            # incident goes back for another look rather than being closed on
            # the strength of having done something.
            IncidentStatus.INVESTIGATING,
            IncidentStatus.SUPERSEDED,
        }
    ),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.VERIFYING, IncidentStatus.CLOSED}),
    IncidentStatus.CLOSED: frozenset(),
    IncidentStatus.SUPERSEDED: frozenset(),
}


class IllegalTransitionError(ValueError):
    """An attempt to move an incident somewhere it cannot go."""

    def __init__(self, current: IncidentStatus, requested: IncidentStatus) -> None:
        allowed = sorted(status.value for status in TRANSITIONS[current])
        super().__init__(
            f"cannot move an incident from {current.value} to {requested.value}; "
            f"legal moves are {allowed or ['(none - terminal)']}"
        )
        self.current = current
        self.requested = requested


def is_legal_transition(current: IncidentStatus, requested: IncidentStatus) -> bool:
    return requested in TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class IncidentOutcome:
    """What a detection did to the incident store."""

    incident: Incident
    #: False when the detection attached to an incident that was already open.
    #: The caller needs this: opening an incident notifies a human, and
    #: attaching to one must not.
    created: bool
    evidence_attached: int

    @property
    def deduplicated(self) -> bool:
        return not self.created


class IncidentService:
    """Applies detections to the incident store."""

    def __init__(
        self,
        incidents: IncidentRepository,
        evidence: EvidenceRepository,
        audit: AuditService,
        *,
        suppression_window: timedelta = DEFAULT_SUPPRESSION_WINDOW,
    ) -> None:
        self._incidents = incidents
        self._evidence = evidence
        self._audit = audit
        self._window = suppression_window

    async def record(
        self,
        detection: Detection,
        *,
        evidence: list[Evidence] | None = None,
        scenario_run_id: str | None = None,
    ) -> IncidentOutcome:
        """Open an incident for this detection, or attach it to a live one.

        Nothing is committed. The caller owns the transaction, so that the
        incident, its evidence and the audit event recording both land
        together or not at all.
        """
        existing = await self._incidents.active_for_correlation_key(detection.correlation_key)
        if existing is not None and self._within_window(existing, detection):
            attached = await self._attach(existing, evidence)
            await self._audit.append(
                actor=detection.detected_by.value,
                action="incident.detection_deduplicated",
                subject_kind="incident",
                subject_id=str(existing.id),
                incident_id=existing.id,
                occurred_at=detection.detected_at,
                payload={
                    "detector": detection.detected_by.value,
                    "detail": detection.detail,
                    "evidence_attached": attached,
                },
            )
            return IncidentOutcome(incident=existing, created=False, evidence_attached=attached)

        incident = await self._incidents.create(
            correlation_key=detection.correlation_key,
            incident_type=detection.incident_type,
            severity=detection.severity,
            entity_kind=detection.entity_kind,
            entity_id=detection.entity_id,
            detected_by=detection.detected_by,
            detected_at=detection.detected_at,
            predicted_breach_at=detection.predicted_breach_at,
            scenario_run_id=scenario_run_id,
        )
        attached = await self._attach(incident, evidence)
        await self._audit.append(
            actor=detection.detected_by.value,
            action="incident.detected",
            subject_kind="incident",
            subject_id=str(incident.id),
            incident_id=incident.id,
            occurred_at=detection.detected_at,
            payload={
                "detector": detection.detected_by.value,
                "incident_type": detection.incident_type,
                "severity": detection.severity.value,
                "detail": detection.detail,
                "evidence_ids": list(detection.evidence_ids),
                "predicted_breach_at": (
                    detection.predicted_breach_at.isoformat()
                    if detection.predicted_breach_at
                    else None
                ),
            },
        )
        return IncidentOutcome(incident=incident, created=True, evidence_attached=attached)

    async def transition(
        self,
        incident: Incident,
        to: IncidentStatus,
        *,
        actor: str,
        at: datetime,
        reason: str | None = None,
    ) -> Incident:
        """Move an incident, refusing anything the table does not allow.

        Raises:
            IllegalTransitionError: The move is not in ``TRANSITIONS``.
        """
        current = IncidentStatus(incident.status)
        if not is_legal_transition(current, to):
            raise IllegalTransitionError(current, to)

        closed_at = at if to in (IncidentStatus.CLOSED, IncidentStatus.RESOLVED) else None
        updated = await self._incidents.set_status(incident.id, to, closed_at=closed_at)
        await self._audit.append(
            actor=actor,
            action="incident.status_changed",
            subject_kind="incident",
            subject_id=str(incident.id),
            incident_id=incident.id,
            occurred_at=at,
            payload={"from": current.value, "to": to.value, "reason": reason},
        )
        return updated

    def _within_window(self, incident: Incident, detection: Detection) -> bool:
        """Whether a live incident is recent enough to absorb this detection.

        Measured from the existing incident's detection time rather than from
        its last update, so that an incident which stays open for days does
        not suppress genuinely new excursions indefinitely.
        """
        return detection.detected_at - incident.detected_at <= self._window

    async def _attach(self, incident: Incident, evidence: list[Evidence] | None) -> int:
        """Persist evidence against an incident.

        Evidence already carrying an incident id is left alone: it belongs to
        whatever claimed it first, and silently reassigning it would rewrite
        the record of what an earlier decision was based on.
        """
        if not evidence:
            return 0
        to_store = [
            item.model_copy(update={"incident_id": incident.id})
            if item.incident_id is None
            else item
            for item in evidence
        ]
        await self._evidence.add_many(to_store)
        return len(to_store)
