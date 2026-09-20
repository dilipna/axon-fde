"""Persistence for outcome verifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.models import OutcomeVerification

__all__ = ["VerificationRepository"]


class VerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def schedule(
        self,
        *,
        incident_id: UUID,
        execution_id: UUID | None,
        expected_effect: dict[str, Any],
        window_start: datetime,
        window_end: datetime,
    ) -> OutcomeVerification:
        verification = OutcomeVerification(
            incident_id=incident_id,
            execution_id=execution_id,
            expected_effect=expected_effect,
            window_start=window_start,
            window_end=window_end,
        )
        self._session.add(verification)
        await self._session.flush()
        return verification

    async def get(self, verification_id: UUID) -> OutcomeVerification | None:
        result = await self._session.execute(
            select(OutcomeVerification).where(OutcomeVerification.id == verification_id)
        )
        return result.scalars().first()

    async def due(self, now: datetime) -> list[OutcomeVerification]:
        """Undecided verifications whose window has closed.

        Ordered oldest window first, so a backlog is worked through in the
        order the actions were taken rather than in whatever order the planner
        happens to return - an incident whose verification was skipped for an
        hour should be looked at before one from a minute ago.
        """
        result = await self._session.execute(
            select(OutcomeVerification)
            .where(OutcomeVerification.verdict.is_(None))
            .where(OutcomeVerification.window_end <= now)
            .order_by(OutcomeVerification.window_end)
        )
        return list(result.scalars().all())

    async def for_incident(self, incident_id: UUID) -> list[OutcomeVerification]:
        result = await self._session.execute(
            select(OutcomeVerification)
            .where(OutcomeVerification.incident_id == incident_id)
            .order_by(OutcomeVerification.created_at)
        )
        return list(result.scalars().all())
