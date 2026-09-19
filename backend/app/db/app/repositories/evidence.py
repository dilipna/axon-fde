"""Persistence for evidence and the links between pieces of it.

The domain model (``backend.app.domain.evidence.Evidence``) is a frozen
Pydantic object with a computed confidence and a nested provenance record. The
table is flat, with JSONB where the shape varies. This module is the only
place that translates between them, so that a change to either shape has one
place to be reflected rather than a dozen.

**Why the value is wrapped.** An observation may be a float, a string, a bool
or a list, and JSONB holds any of those - but a bare JSONB scalar leaves no
room to add a qualifier later without rewriting every stored row. It is stored
as ``{"value": ...}`` for that headroom.

**Why round-tripping does not recompute confidence.** ``Evidence.create()``
derives confidence from the observation's age at ingestion. Reloading a row an
hour later and re-deriving would silently change a stored number that an
approval may already be bound to, so the stored confidence is restored as-is.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.models import Evidence as EvidenceRow
from backend.app.db.app.models import EvidenceLink as EvidenceLinkRow
from backend.app.domain.enums import (
    ConfidenceBasis,
    EvidenceRelation,
    EvidenceSource,
    EvidenceStatus,
    FreshnessState,
    Modality,
)
from backend.app.domain.evidence import (
    EntityRef,
    Evidence,
    EvidenceLink,
    ObservationValue,
    Provenance,
)

__all__ = ["EvidenceRepository", "to_domain", "to_row"]


def to_row(evidence: Evidence) -> EvidenceRow:
    """Flatten a domain observation into its table row."""
    return EvidenceRow(
        id=evidence.id,
        incident_id=evidence.incident_id,
        entity_kind=evidence.entity_ref.kind,
        entity_id=evidence.entity_ref.id,
        source=evidence.source.value,
        modality=evidence.modality.value,
        observation_type=evidence.observation_type,
        value={"value": evidence.value},
        unit=evidence.unit,
        observed_at=evidence.observed_at,
        valid_from=evidence.valid_from,
        valid_to=evidence.valid_to,
        ingested_at=evidence.ingested_at,
        confidence=evidence.confidence,
        confidence_basis=evidence.confidence_basis.value,
        freshness=evidence.freshness.value,
        # `mode="json"` so the UUIDs in `derived_from` and the bbox tuple
        # become JSON-native types. Without it the driver rejects the payload.
        provenance=evidence.provenance.model_dump(mode="json", exclude_none=True),
        artifact_id=evidence.artifact_id,
        content_hash=evidence.content_hash,
        status=evidence.status.value,
    )


def to_domain(row: EvidenceRow) -> Evidence:
    """Rebuild the domain observation from its row.

    Constructed directly rather than through ``Evidence.create()``: that
    classmethod recomputes confidence and freshness from the current clock,
    which is right on ingestion and wrong on read.
    """
    stored: dict[str, Any] = row.value
    return Evidence(
        id=row.id,
        incident_id=row.incident_id,
        entity_ref=EntityRef(kind=row.entity_kind, id=row.entity_id),
        source=EvidenceSource(row.source),
        modality=Modality(row.modality),
        observation_type=row.observation_type,
        value=cast(ObservationValue, stored["value"]),
        unit=row.unit,
        observed_at=row.observed_at,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        ingested_at=row.ingested_at,
        confidence=row.confidence,
        confidence_basis=ConfidenceBasis(row.confidence_basis),
        freshness=FreshnessState(row.freshness),
        provenance=Provenance.model_validate(row.provenance),
        artifact_id=row.artifact_id,
        content_hash=row.content_hash,
        status=EvidenceStatus(row.status),
    )


class EvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, evidence: Evidence) -> Evidence:
        """Store one observation and return what was stored."""
        row = to_row(evidence)
        self._session.add(row)
        await self._session.flush()
        return to_domain(row)

    async def add_many(self, evidence: list[Evidence]) -> list[Evidence]:
        """Store a batch in one flush.

        A telemetry replay inserts thousands of readings; a flush per row turns
        a scenario replay into a round-trip-bound crawl.
        """
        rows = [to_row(item) for item in evidence]
        self._session.add_all(rows)
        await self._session.flush()
        return [to_domain(row) for row in rows]

    async def get(self, evidence_id: UUID) -> Evidence | None:
        row = await self._session.get(EvidenceRow, evidence_id)
        return to_domain(row) if row is not None else None

    async def for_incident(self, incident_id: UUID) -> list[Evidence]:
        rows = (
            (
                await self._session.execute(
                    select(EvidenceRow)
                    .where(EvidenceRow.incident_id == incident_id)
                    .order_by(EvidenceRow.observed_at)
                )
            )
            .scalars()
            .all()
        )
        return [to_domain(row) for row in rows]

    async def active_observations(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        observation_type: str | None = None,
    ) -> list[Evidence]:
        """Active observations about one entity.

        This is the reconciliation lookup: only active evidence can be in
        conflict, because a superseded reading has already been resolved. The
        ``ix_evidence_reconcile`` index exists for exactly this query.
        """
        statement = select(EvidenceRow).where(
            EvidenceRow.entity_kind == entity_kind,
            EvidenceRow.entity_id == entity_id,
            EvidenceRow.status == EvidenceStatus.ACTIVE.value,
        )
        if observation_type is not None:
            statement = statement.where(EvidenceRow.observation_type == observation_type)
        result = await self._session.execute(statement.order_by(EvidenceRow.observed_at))
        return [to_domain(row) for row in result.scalars().all()]

    async def mark_status(self, evidence_id: UUID, status: EvidenceStatus) -> None:
        """Change an observation's status.

        Evidence is mutable in exactly this one field. The reading itself never
        changes - a superseded observation is retained with its original value,
        because an audit of what was known at the time needs the row that was
        acted on, not the one that replaced it.
        """
        row = await self._session.get(EvidenceRow, evidence_id)
        if row is None:
            raise KeyError(f"no evidence with id {evidence_id}")
        row.status = status.value
        await self._session.flush()

    async def add_link(self, link: EvidenceLink) -> EvidenceLink:
        row = EvidenceLinkRow(
            id=link.id,
            from_evidence_id=link.from_evidence_id,
            to_evidence_id=link.to_evidence_id,
            relation=link.relation.value,
            detected_by=link.detected_by,
            rule_id=link.rule_id,
            detail=link.detail,
        )
        self._session.add(row)
        await self._session.flush()
        return link

    async def links_for(self, evidence_id: UUID) -> list[EvidenceLink]:
        """Every link touching this observation, in either direction."""
        result = await self._session.execute(
            select(EvidenceLinkRow).where(
                (EvidenceLinkRow.from_evidence_id == evidence_id)
                | (EvidenceLinkRow.to_evidence_id == evidence_id)
            )
        )
        return [
            EvidenceLink(
                id=row.id,
                from_evidence_id=row.from_evidence_id,
                to_evidence_id=row.to_evidence_id,
                relation=EvidenceRelation(row.relation),
                detected_by=row.detected_by,
                rule_id=row.rule_id,
                detail=row.detail,
            )
            for row in result.scalars().all()
        ]
