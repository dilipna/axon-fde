"""Persistence for model invocations.

Makes cost per incident a query rather than a spreadsheet, and ties every
model output to the exact prompt version that produced it.

``cache_read_tokens`` is stored on every row for one reason: it is the number
that says whether prompt caching is working. Zero across repeated incidents
means a silent invalidator crept into the prefix, and that is where the cost
budget actually leaks - invisibly, and weeks before the bill shows it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.models import ModelInvocation
from backend.app.llm.provider import LLMRequest, LLMResponse

__all__ = ["InvocationRepository"]


class InvocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        request: LLMRequest,
        response: LLMResponse,
        *,
        incident_id: UUID | None = None,
    ) -> ModelInvocation:
        """Store one call.

        The model is taken from the *response*, not the request. A server-side
        fallback can answer on a different model than the one asked for, and
        costing the call at the requested model's price would understate it.
        """
        invocation = ModelInvocation(
            incident_id=incident_id,
            node=request.node,
            model_id=response.model,
            prompt_version=request.prompt_version,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cost_estimate_usd=response.cost_usd(),
            latency_ms=response.latency_ms,
        )
        self._session.add(invocation)
        await self._session.flush()
        return invocation

    async def for_incident(self, incident_id: UUID) -> list[ModelInvocation]:
        result = await self._session.execute(
            select(ModelInvocation)
            .where(ModelInvocation.incident_id == incident_id)
            .order_by(ModelInvocation.created_at)
        )
        return list(result.scalars().all())

    async def cost_for_incident(self, incident_id: UUID) -> float:
        """Total spend on one incident, in dollars.

        A query rather than a sum in application code, because the question
        "what did this incident cost?" is asked of the database by people who
        are not running the application.
        """
        total = await self._session.scalar(
            select(func.coalesce(func.sum(ModelInvocation.cost_estimate_usd), 0.0)).where(
                ModelInvocation.incident_id == incident_id
            )
        )
        return float(total or 0.0)
