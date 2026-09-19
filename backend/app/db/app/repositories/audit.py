"""Persistence for the audit chain.

The chain itself is pure and lives in ``backend/app/audit/chain.py``. This
module is the part that touches the database, and it exists mainly to hold one
thing correctly: the lock that makes concurrent appends safe.

**Why appending needs a lock, and what actually goes wrong without one.** An
append reads the current head and writes a row that points at it. Two appends
running at once read the same head and both claim the next sequence number.

The obvious fear is a forked chain, and that is not what happens here: the
unique constraint on ``seq`` refuses the second writer. Measured rather than
assumed - with the lock removed, twelve simultaneous appends leave one event
stored and eleven ``UniqueViolationError``s.

So the constraint already prevents the fork, and the failure mode is losing
audit events instead. For this table that is the worse of the two. A forked
chain is at least visible; a rejected append means a consequential action
happened with nothing in the record to say so, and the chain that remains
verifies perfectly. The lock is what turns contention into a queue rather than
into silence.

**Why an advisory lock rather than ``SELECT ... FOR UPDATE``.** Locking the
head row is the obvious answer and it is wrong in the one case that matters:
an empty chain has no head row, so the first two appends after a deployment
lock nothing and race. A transaction-scoped advisory lock is taken on a name
rather than on a row, so it works identically whether the table holds zero
rows or a million.

The lock is released when the transaction commits or rolls back. Nothing has
to remember to release it, which matters because a leaked chain lock would
stall every subsequent append in the process.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit.chain import GENESIS_HASH, AuditRecord
from backend.app.db.app.models import AuditEvent

__all__ = ["CHAIN_LOCK_KEY", "AuditRepository"]


def _lock_key(name: str) -> int:
    """A stable 64-bit advisory lock key derived from a name.

    Derived from a digest rather than Python's ``hash()``, which is salted per
    process and would hand two workers different keys - that is, no lock at
    all. Signed, because ``pg_advisory_xact_lock`` takes a ``bigint``.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


#: Every appender in every process must agree on this value or the lock is
#: decorative.
CHAIN_LOCK_KEY = _lock_key("axon.audit_event.chain")


class AuditRepository:
    """Reads and appends to the audit chain. Never updates or deletes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_chain(self) -> None:
        """Serialise appends for the remainder of this transaction.

        Blocks rather than failing when another appender holds the lock: an
        audit append that gave up under contention would mean a consequential
        action happening with no record of it, which is the one outcome this
        table exists to prevent.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK_KEY}
        )

    async def head(self) -> tuple[int, str]:
        """The sequence number and hash of the last event.

        Returns ``(0, GENESIS_HASH)`` for an empty chain, so the caller has no
        special case and the first event is numbered 1.

        Callers must hold the chain lock before relying on this, or the answer
        is stale by the time they act on it.
        """
        row = (
            await self._session.execute(
                select(AuditEvent.seq, AuditEvent.event_hash)
                .order_by(AuditEvent.seq.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return 0, GENESIS_HASH
        return int(row[0]), str(row[1])

    async def insert(self, record: AuditRecord, event_hash: str) -> AuditEvent:
        """Write one event. The only write path this table has."""
        event = AuditEvent(
            seq=record.seq,
            prev_hash=record.prev_hash,
            event_hash=event_hash,
            actor=record.actor,
            actor_role=record.actor_role,
            action=record.action,
            subject_kind=record.subject_kind,
            subject_id=record.subject_id,
            incident_id=UUID(record.incident_id) if record.incident_id else None,
            payload=record.payload,
            occurred_at=record.occurred_at,
        )
        self._session.add(event)
        # Flushed rather than left to the caller's commit so that a unique
        # violation on `seq` surfaces here, attributable to this append,
        # instead of at commit time alongside unrelated work.
        await self._session.flush()
        return event

    async def rows(self, *, incident_id: UUID | None = None) -> list[AuditEvent]:
        """Every event in chain order, optionally narrowed to one incident.

        Ordered by ``seq``, never by ``occurred_at``: two events in the same
        millisecond have no defined order by timestamp, and a chain walked out
        of order reports breaks that are not there.
        """
        statement = select(AuditEvent).order_by(AuditEvent.seq)
        if incident_id is not None:
            statement = statement.where(AuditEvent.incident_id == incident_id)
        return list((await self._session.execute(statement)).scalars().all())

    async def load_chain(self) -> tuple[list[AuditRecord], list[str]]:
        """The chain in the shape ``verify_chain`` expects."""
        events = await self.rows()
        return [to_record(event) for event in events], [event.event_hash for event in events]

    async def count(self) -> int:
        return len(await self.rows())


def to_record(event: AuditEvent) -> AuditRecord:
    """Rebuild the hashable content of a stored event.

    ``id`` and ``created_at`` are excluded, matching ``AuditRecord``: they are
    storage details, and hashing them would make the chain unverifiable after
    a migration that reassigned identifiers.
    """
    payload: dict[str, Any] = event.payload
    occurred_at: datetime = event.occurred_at
    return AuditRecord(
        seq=event.seq,
        actor=event.actor,
        actor_role=event.actor_role,
        action=event.action,
        subject_kind=event.subject_kind,
        subject_id=event.subject_id,
        incident_id=str(event.incident_id) if event.incident_id else None,
        payload=payload,
        occurred_at=occurred_at,
        prev_hash=event.prev_hash,
    )
