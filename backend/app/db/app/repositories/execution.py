"""Persistence for action executions.

Holds one thing carefully: the lock that makes idempotency real.

**Why a unique constraint is not enough on its own.** ``idempotency_key`` is
unique, so two concurrent executions cannot both store a row. But the loser
finds out by raising ``IntegrityError``, which in SQLAlchemy poisons the
surrounding transaction - and that transaction is also carrying the audit
event for the execution. Recovering means rolling back and starting again,
which is a lot of machinery for a case that a lock removes entirely.

So the key is locked before it is looked up, exactly as the audit chain locks
before reading its head, and for the same reason: turning contention into a
queue rather than into an error. The constraint stays as the backstop for any
path that forgets to lock.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.models import ActionExecution

__all__ = ["ExecutionRepository"]


def _lock_key(idempotency_key: str) -> int:
    """A stable 64-bit advisory lock key for one idempotency key.

    Derived from a digest rather than Python's ``hash()``, which is salted per
    process: two workers would compute different keys and lock nothing. Signed
    because ``pg_advisory_xact_lock`` takes a ``bigint``.

    Namespaced so that an idempotency key cannot collide with the audit
    chain's lock and stall every append in the process.
    """
    digest = hashlib.blake2b(
        f"axon.action_execution:{idempotency_key}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class ExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock(self, idempotency_key: str) -> None:
        """Serialise executions of this key for the rest of the transaction.

        Per key rather than global: two different actions on two different
        incidents have no reason to queue behind each other, and a global lock
        would serialise the whole fleet through one executor.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key(idempotency_key)}
        )

    async def by_idempotency_key(self, idempotency_key: str) -> ActionExecution | None:
        result = await self._session.execute(
            select(ActionExecution).where(ActionExecution.idempotency_key == idempotency_key)
        )
        return result.scalars().first()

    async def record(
        self,
        *,
        incident_id: UUID,
        approval_id: UUID | None,
        idempotency_key: str,
        action_type: str,
        request: dict[str, Any],
        status: str,
        result: dict[str, Any] | None,
        executed_at: datetime,
    ) -> ActionExecution:
        execution = ActionExecution(
            incident_id=incident_id,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            action_type=action_type,
            request=request,
            status=status,
            attempts=1,
            result=result,
            executed_at=executed_at,
        )
        self._session.add(execution)
        await self._session.flush()
        return execution

    async def note_repeat_attempt(self, execution: ActionExecution) -> ActionExecution:
        """Count a retry without repeating the side effect.

        The counter is the only thing a replay changes. ``result`` and
        ``executed_at`` keep their original values deliberately: the question
        an auditor asks is when the action happened, not when somebody last
        asked about it, and a retry that refreshed the timestamp would move a
        reroute an hour later than it occurred.
        """
        execution.attempts += 1
        await self._session.flush()
        return execution

    async def for_incident(self, incident_id: UUID) -> list[ActionExecution]:
        result = await self._session.execute(
            select(ActionExecution)
            .where(ActionExecution.incident_id == incident_id)
            .order_by(ActionExecution.created_at)
        )
        return list(result.scalars().all())
