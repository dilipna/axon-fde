"""Persistence for incidents.

Deliberately thin. The incident *lifecycle* - which transitions are legal,
what each one requires, and how a detection is deduplicated against a live
incident - is a decision layer, not a storage one, and it is built in the next
block. What lives here is the storage and the one query that layer will be
built on: find the live incident for a correlation key.

``Incident`` has no separate domain model yet, so this returns ORM rows rather
than translating. That is honest about the current state: inventing a domain
type with no behaviour on it would be ceremony, and a premature one would
constrain the state machine before it is designed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.models import Incident
from backend.app.domain.enums import (
    ACTIVE_INCIDENT_STATUSES,
    DetectedBy,
    IncidentSeverity,
    IncidentStatus,
)

__all__ = ["IncidentRepository"]


class IncidentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        correlation_key: str,
        incident_type: str,
        severity: IncidentSeverity,
        entity_kind: str,
        entity_id: str,
        detected_by: DetectedBy,
        detected_at: datetime,
        status: IncidentStatus = IncidentStatus.DETECTED,
        predicted_breach_at: datetime | None = None,
        scenario_run_id: str | None = None,
    ) -> Incident:
        """Open an incident.

        Callers wanting deduplication use ``active_for_correlation_key`` first.
        Creation does not deduplicate on its own, because whether a matching
        live incident should absorb a new detection depends on a suppression
        window that only the detector knows.
        """
        if detected_at.tzinfo is None:
            raise ValueError(
                "detected_at must be timezone-aware; lead time is measured "
                "against it and a naive timestamp makes that measurement "
                "depend on the server's timezone"
            )
        incident = Incident(
            correlation_key=correlation_key,
            status=status.value,
            severity=severity.value,
            incident_type=incident_type,
            entity_kind=entity_kind,
            entity_id=entity_id,
            detected_by=detected_by.value,
            detected_at=detected_at,
            predicted_breach_at=predicted_breach_at,
            scenario_run_id=scenario_run_id,
        )
        self._session.add(incident)
        await self._session.flush()
        return incident

    async def get(self, incident_id: UUID) -> Incident | None:
        return await self._session.get(Incident, incident_id)

    async def active_for_correlation_key(self, correlation_key: str) -> Incident | None:
        """The live incident for this key, if there is one.

        The deduplication lookup, served by ``ix_incident_correlation_active``.
        Returns the most recently detected match, so that a key which somehow
        has two live incidents attaches to the current one rather than to a
        stale straggler.
        """
        statement = (
            select(Incident)
            .where(
                Incident.correlation_key == correlation_key,
                Incident.status.in_([status.value for status in ACTIVE_INCIDENT_STATUSES]),
            )
            .order_by(Incident.detected_at.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def set_status(
        self,
        incident_id: UUID,
        status: IncidentStatus,
        *,
        closed_at: datetime | None = None,
    ) -> Incident:
        """Move an incident to a new status.

        No transition validation here on purpose - see the module docstring.
        The state machine that owns those rules calls this to persist a
        transition it has already decided is legal.
        """
        incident = await self._session.get(Incident, incident_id)
        if incident is None:
            raise KeyError(f"no incident with id {incident_id}")
        incident.status = status.value
        if closed_at is not None:
            incident.closed_at = closed_at
        await self._session.flush()
        return incident

    async def supersede(self, incident_id: UUID, *, by: UUID, at: datetime) -> Incident:
        """Absorb one incident into another.

        Records the absorbing incident rather than only closing this one, so
        that the evidence and audit trail attached to the superseded incident
        remain reachable from the surviving one. An auditor asking why forty
        readings produced one incident needs that link to exist.
        """
        incident = await self.set_status(incident_id, IncidentStatus.SUPERSEDED, closed_at=at)
        incident.superseded_by = by
        await self._session.flush()
        return incident

    async def open_incidents(self) -> list[Incident]:
        """Every live incident, most recent first."""
        statement = (
            select(Incident)
            .where(Incident.status.in_([status.value for status in ACTIVE_INCIDENT_STATUSES]))
            .order_by(Incident.detected_at.desc())
        )
        return list((await self._session.execute(statement)).scalars().all())
