"""Persistence for risk assessments.

Short, because the interesting constraint is not here: ``RiskEstimate``
already cannot exist without a baseline, so this repository has no way to
write a prediction without one. The ``NOT NULL`` on
``risk_assessment.baseline_probability`` is the third layer under that, after
the dataclass and the service.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.models import RiskAssessment
from backend.app.risk.service import RiskEstimate

__all__ = ["RiskRepository"]


class RiskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, incident_id: UUID, estimate: RiskEstimate) -> RiskAssessment:
        """Store an assessment against an incident.

        The feature hash is stored, not the features. Storing the vector
        itself would be a second copy of data that the evidence rows already
        hold, and it would drift from them; the hash is enough to prove which
        inputs produced this number.
        """
        assessment = RiskAssessment(
            incident_id=incident_id,
            horizon_minutes=estimate.horizon_minutes,
            probability=estimate.probability,
            baseline_probability=estimate.baseline_probability,
            baseline_name=estimate.baseline_name,
            model_version=estimate.model_version,
            feature_vector_hash=estimate.feature_vector_hash,
            degraded=estimate.degraded,
        )
        self._session.add(assessment)
        await self._session.flush()
        return assessment

    async def for_incident(self, incident_id: UUID) -> list[RiskAssessment]:
        """Every assessment for one incident, oldest first.

        Ordered so the risk trajectory is readable: an incident whose
        probability climbed steadily reads very differently from one that
        spiked once, and an auditor asking "when did you know?" is asking
        about that sequence.
        """
        result = await self._session.execute(
            select(RiskAssessment)
            .where(RiskAssessment.incident_id == incident_id)
            .order_by(RiskAssessment.created_at)
        )
        return list(result.scalars().all())

    async def latest_for_incident(self, incident_id: UUID) -> RiskAssessment | None:
        result = await self._session.execute(
            select(RiskAssessment)
            .where(RiskAssessment.incident_id == incident_id)
            .order_by(RiskAssessment.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()
