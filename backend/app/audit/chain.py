"""The tamper-evident audit chain.

Axon's Compliance Lead described the problem unprompted: after a reroute,
nobody records whether it worked, and when a pharma client asks "why did you
continue to the destination rather than divert?", there is no answer. An
immutable, evidence-linked record of what was known, what was decided, by
whom, and what happened next is the product that answers that question.

The chain is what makes it *tamper-evident* rather than merely *stored*. Each
event hashes the previous one, so altering a historical row breaks the link to
everything after it, and `verify_chain` says exactly where.

**What a hash chain does and does not protect against.** This distinction
matters and is easy to get wrong:

- An attacker who edits one row and stops is detected immediately.
- An attacker who edits a row *and recomputes every hash after it* produces a
  chain that verifies cleanly. An unkeyed hash chain does not survive a full
  rewrite by someone with write access. Claiming otherwise would be false
  assurance.

Three things close that gap, and the system uses all three:

1. **Keyed hashing.** When `AXON_AUDIT_HMAC_KEY` is configured, events are
   chained with HMAC-SHA256 rather than plain SHA-256. The key is held by the
   application, never by the database role, so an attacker with database write
   access cannot compute a valid hash for a row they forged. This is the
   control that actually defeats the rewrite attack.
2. **Append-only grants.** The application role holds INSERT and SELECT only,
   and a trigger raises on UPDATE or DELETE, so the rewrite cannot happen
   through the application's own credentials at all.
3. **An anchored head.** Truncating the chain leaves an internally consistent
   prefix, so the head hash is checkpointed outside the table. Without that
   anchor, dropping the last N events is undetectable.

This module is pure. Persistence and the database-level protections live
alongside it in `service.py` and the migration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = [
    "GENESIS_HASH",
    "ChainBreak",
    "ChainVerification",
    "compute_event_hash",
    "verify_chain",
]

#: The predecessor of the first event. All zeroes rather than a random value,
#: so an empty chain is verifiable from nothing but this constant.
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """The hashable content of one audit event.

    Deliberately excludes the row's own `id` and `created_at`: those are
    storage details, and including them would make a chain unverifiable after
    a legitimate migration that reassigned identifiers.
    """

    seq: int
    actor: str
    actor_role: str | None
    action: str
    subject_kind: str
    subject_id: str
    incident_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime
    prev_hash: str


def compute_event_hash(record: AuditRecord, key: bytes | None = None) -> str:
    """Hash one event over its content and its predecessor.

    Canonical JSON with sorted keys, so the same logical event always hashes
    identically regardless of dictionary ordering. Timestamps are serialised
    in ISO-8601 with an explicit offset, so a chain verifies the same way in
    any timezone.

    With `key`, this is HMAC-SHA256 rather than SHA-256. The key is held by
    the application and never by the database role, which is what stops an
    attacker with database write access from recomputing a chain around a
    forged row. Without a key the chain still detects partial tampering, but
    not a full rewrite - see the module docstring.
    """
    canonical = json.dumps(
        {
            "seq": record.seq,
            "actor": record.actor,
            "actor_role": record.actor_role,
            "action": record.action,
            "subject_kind": record.subject_kind,
            "subject_id": record.subject_id,
            "incident_id": record.incident_id,
            "payload": record.payload,
            "occurred_at": record.occurred_at.isoformat(),
            "prev_hash": record.prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    payload = canonical.encode("utf-8")
    if key is not None:
        return hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """Where and how the chain failed to verify."""

    seq: int
    reason: str
    expected: str
    found: str

    def describe(self) -> str:
        return (
            f"seq {self.seq}: {self.reason} "
            f"(expected {self.expected[:16]}..., found {self.found[:16]}...)"
        )


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """The result of walking a chain."""

    valid: bool
    events_checked: int
    breaks: tuple[ChainBreak, ...] = ()

    #: The hash of the last verified event, which the next append links to.
    head_hash: str = GENESIS_HASH

    def describe(self) -> str:
        if self.valid:
            return f"chain verified: {self.events_checked} events intact"
        return f"chain BROKEN at {len(self.breaks)} point(s): " + "; ".join(
            b.describe() for b in self.breaks
        )


def verify_chain(
    records: list[AuditRecord],
    stored_hashes: list[str],
    key: bytes | None = None,
) -> ChainVerification:
    """Walk a chain and report every break.

    Reports *all* breaks rather than stopping at the first. An attacker who
    edits one row and recomputes the hashes after it produces a single break;
    one who edits a row and leaves the rest alone produces many. The shape of
    the damage is itself evidence, so it is worth showing in full.

    Three failure modes are distinguished:

    - `sequence_gap`      an event is missing from the chain
    - `broken_link`       an event does not point at its predecessor
    - `content_tampered`  an event's stored hash does not match its content
    """
    if len(records) != len(stored_hashes):
        raise ValueError(
            f"{len(records)} records but {len(stored_hashes)} hashes; "
            "the chain cannot be verified from mismatched inputs"
        )

    if not records:
        return ChainVerification(valid=True, events_checked=0, head_hash=GENESIS_HASH)

    breaks: list[ChainBreak] = []
    previous_hash = GENESIS_HASH
    expected_seq = records[0].seq

    for record, stored in zip(records, stored_hashes, strict=True):
        if record.seq != expected_seq:
            breaks.append(
                ChainBreak(
                    seq=record.seq,
                    reason="sequence_gap",
                    expected=str(expected_seq),
                    found=str(record.seq),
                )
            )
            expected_seq = record.seq

        if record.prev_hash != previous_hash:
            breaks.append(
                ChainBreak(
                    seq=record.seq,
                    reason="broken_link",
                    expected=previous_hash,
                    found=record.prev_hash,
                )
            )

        recomputed = compute_event_hash(record, key)
        if recomputed != stored:
            breaks.append(
                ChainBreak(
                    seq=record.seq,
                    reason="content_tampered",
                    expected=recomputed,
                    found=stored,
                )
            )

        # Continue from the stored hash rather than the recomputed one, so a
        # single tampered row produces one break instead of cascading through
        # every event after it and obscuring where the damage actually starts.
        previous_hash = stored
        expected_seq += 1

    return ChainVerification(
        valid=not breaks,
        events_checked=len(records),
        breaks=tuple(breaks),
        head_hash=stored_hashes[-1],
    )


def next_link(
    *,
    seq: int,
    actor: str,
    actor_role: str | None,
    action: str,
    subject_kind: str,
    subject_id: str,
    incident_id: str | None,
    payload: dict[str, Any],
    occurred_at: datetime,
    prev_hash: str,
    key: bytes | None = None,
) -> tuple[AuditRecord, str]:
    """Build the next event in a chain and its hash."""
    record = AuditRecord(
        seq=seq,
        actor=actor,
        actor_role=actor_role,
        action=action,
        subject_kind=subject_kind,
        subject_id=subject_id,
        incident_id=incident_id,
        payload=payload,
        occurred_at=occurred_at,
        prev_hash=prev_hash,
    )
    return record, compute_event_hash(record, key)
