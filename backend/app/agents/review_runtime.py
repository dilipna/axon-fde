"""Persisted review adapter over Axon's existing authorization and action executor.

Constructed by trusted application wiring with an authenticated Principal and a
fresh-context loader. No role, SQL text or executor is taken from a resume body.
Each effect has its own committed transaction before the graph checkpoints its
receipt, so a retry uses ActionExecutor's approval-derived idempotency identity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.actions.executor import ActionExecutor, ExecutionRefusedError
from backend.app.agents.review import ApprovalRecord, ReviewGate
from backend.app.agents.state import IncidentState
from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import ApprovalService
from backend.app.audit.service import AuditService
from backend.app.policies.engine import Principal


class DatabaseReviewRuntime:
    """Reuse actual role, binding, audit, locking and idempotency enforcement."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        principal: Principal,
        load_context: Callable[[IncidentState], Awaitable[ApprovalContext]],
        request_for: Callable[[IncidentState, ApprovalContext], dict[str, Any]],
        now: Callable[[], datetime],
        audit_key: bytes | None = None,
    ) -> None:
        self._sessions = sessions
        self._principal = principal
        self._context = load_context
        self._request = request_for
        self._now = now
        self._audit_key = audit_key

    def gate(self) -> ReviewGate:
        return ReviewGate(
            load_context=self._context,
            load_approval=self.load_approval,
            execute_approved=self.execute,
            now=self._now,
        )

    async def load_approval(self, approval_id: UUID) -> ApprovalRecord | None:
        async with self._sessions() as session:
            service = ApprovalService(session, AuditService(session, key=self._audit_key))
            return await service.get(approval_id)

    async def execute(
        self,
        state: IncidentState,
        approval_id: UUID,
        context: ApprovalContext,
    ) -> dict[str, Any]:
        refusal: ExecutionRefusedError | None = None
        receipt: dict[str, Any] | None = None
        async with self._sessions() as session, session.begin():
            current = await self._context(state)
            if current.bound_hash() != context.bound_hash():
                raise ValueError("evidence changed before executor handoff")
            audit = AuditService(session, key=self._audit_key)
            approvals = ApprovalService(session, audit)
            record = await approvals.get(approval_id)
            executor = ActionExecutor(session, audit, approvals)
            try:
                outcome = await executor.execute(
                    principal=self._principal,
                    context=current,
                    request=self._request(state, current),
                    approval=record,
                    at=self._now(),
                )
            except ExecutionRefusedError as exc:
                # Commit the persisted refusal audit, then surface failure to
                # the graph. Rolling it back would erase the rejected attempt.
                refusal = exc
            else:
                receipt = {
                    "execution_id": str(outcome.execution.id),
                    "approval_id": str(approval_id),
                    "incident_id": str(current.incident_id),
                    "context_hash": current.bound_hash(),
                    "replayed": outcome.replayed,
                    "status": outcome.execution.status,
                    "summary": outcome.summary,
                    "scope": "local simulated carrier; persisted ActionExecutor result",
                }
        if refusal is not None:
            raise refusal
        if receipt is None:
            raise RuntimeError("executor returned no receipt")
        return receipt
