"""Appending to and verifying the audit chain.

This is the one supported way to write an audit event. It owns three things
that are easy to get wrong separately and impossible to get wrong here:

1. The chain lock is taken before the head is read, so concurrent appends
   serialise instead of forking.
2. The sequence number and ``prev_hash`` come from the head that was read
   under that lock, not from the caller.
3. The HMAC key is applied consistently to writes and to verification, so a
   chain written keyed never appears broken merely because verification forgot
   the key.

The caller supplies *what happened*. It never supplies ``seq``, ``prev_hash``
or ``event_hash``, for the same reason nothing supplies its own confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit.chain import ChainVerification, next_link, verify_chain
from backend.app.config import Settings, get_settings
from backend.app.db.app.models import AuditEvent
from backend.app.db.app.repositories.audit import AuditRepository

__all__ = ["AuditService"]


class AuditService:
    """Append-only writer for the audit chain."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        key: bytes | None = None,
    ) -> None:
        self._session = session
        self._repo = AuditRepository(session)
        # An explicit key wins, so tests and the chain verifier can be pointed
        # at a specific key without mutating process-wide settings.
        self._key = key if key is not None else (settings or get_settings()).audit_hmac_key()

    @property
    def is_keyed(self) -> bool:
        """Whether this chain resists a full rewrite.

        Exposed so that a verification report can say which guarantee it is
        actually making rather than implying the stronger one.
        """
        return self._key is not None

    async def append(
        self,
        *,
        actor: str,
        action: str,
        subject_kind: str,
        subject_id: str,
        payload: dict[str, Any] | None = None,
        actor_role: str | None = None,
        incident_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditEvent:
        """Append one event and return the stored row.

        The write is not committed here. An audit event describes something
        the surrounding transaction did, so it must commit or roll back with
        that work; committing separately would record actions that were then
        rolled back, and lose the record of actions that succeeded.
        """
        # Taken before the head read, which is the whole point. Held until the
        # caller's transaction ends.
        await self._repo.lock_chain()
        last_seq, head_hash = await self._repo.head()

        moment = occurred_at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError(
                "occurred_at must be timezone-aware; a naive timestamp hashes "
                "differently depending on the server's timezone and would make "
                "the chain unverifiable elsewhere"
            )

        record, event_hash = next_link(
            seq=last_seq + 1,
            actor=actor,
            actor_role=actor_role,
            action=action,
            subject_kind=subject_kind,
            subject_id=subject_id,
            incident_id=str(incident_id) if incident_id else None,
            payload=payload or {},
            occurred_at=moment,
            prev_hash=head_hash,
            key=self._key,
        )
        return await self._repo.insert(record, event_hash)

    async def verify(self) -> ChainVerification:
        """Walk the stored chain and report every break."""
        records, hashes = await self._repo.load_chain()
        return verify_chain(records, hashes, self._key)
