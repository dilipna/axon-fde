"""Tests for the tamper-evident audit chain.

The chain is the compliance product (customer brief, opportunity O2) and the
control behind threat T13. Tampering is the threat that most directly attacks
the product's value: a decision record nobody can trust is worth nothing to an
auditor.

The tests below are written as attacks. Each one alters a stored chain the way
someone hiding a bad decision would, and asserts that verification catches it
and says where.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.audit.chain import (
    GENESIS_HASH,
    AuditRecord,
    compute_event_hash,
    next_link,
    verify_chain,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def build_chain(count: int = 5, *, key: bytes | None = None) -> tuple[list[AuditRecord], list[str]]:
    """A valid chain of `count` events, as it would be stored."""
    records: list[AuditRecord] = []
    hashes: list[str] = []
    prev = GENESIS_HASH

    for index in range(count):
        record, digest = next_link(
            seq=index + 1,
            actor="dispatcher@axon.example",
            actor_role="dispatcher",
            action=[
                "incident_created",
                "evidence_added",
                "risk_assessed",
                "approval_requested",
                "action_executed",
            ][index % 5],
            subject_kind="incident",
            subject_id="INC-001",
            incident_id="INC-001",
            payload={"step": index, "detail": f"event {index}"},
            occurred_at=NOW + timedelta(minutes=index),
            prev_hash=prev,
            key=key,
        )
        records.append(record)
        hashes.append(digest)
        prev = digest

    return records, hashes


# ---------------------------------------------------------------------------
# A well-formed chain verifies
# ---------------------------------------------------------------------------


def test_a_valid_chain_verifies():
    records, hashes = build_chain()
    result = verify_chain(records, hashes)

    assert result.valid
    assert result.events_checked == 5
    assert not result.breaks
    assert result.head_hash == hashes[-1]


def test_an_empty_chain_verifies_from_genesis():
    result = verify_chain([], [])
    assert result.valid
    assert result.head_hash == GENESIS_HASH


def test_the_first_event_links_to_genesis():
    records, _ = build_chain(1)
    assert records[0].prev_hash == GENESIS_HASH


def test_hashing_is_deterministic():
    records, hashes = build_chain(3)
    assert [compute_event_hash(r) for r in records] == hashes


def test_key_ordering_does_not_change_a_hash():
    """Canonical JSON: the same logical event always hashes identically."""
    base, digest = next_link(
        seq=1,
        actor="a",
        actor_role="dispatcher",
        action="x",
        subject_kind="incident",
        subject_id="INC-1",
        incident_id="INC-1",
        payload={"alpha": 1, "beta": 2},
        occurred_at=NOW,
        prev_hash=GENESIS_HASH,
    )
    reordered = replace(base, payload={"beta": 2, "alpha": 1})
    assert compute_event_hash(reordered) == digest


# ---------------------------------------------------------------------------
# Attacks
# ---------------------------------------------------------------------------


def test_editing_a_payload_is_detected():
    """The core attack: change what the record says happened."""
    records, hashes = build_chain()

    # Someone rewrites the third event to hide what was actually decided.
    records[2] = replace(records[2], payload={"step": 2, "detail": "nothing to see"})

    result = verify_chain(records, hashes)
    assert not result.valid
    assert any(b.reason == "content_tampered" and b.seq == 3 for b in result.breaks)


def test_editing_the_actor_is_detected():
    """Reassigning blame for a decision."""
    records, hashes = build_chain()
    records[1] = replace(records[1], actor="someone.else@axon.example")

    result = verify_chain(records, hashes)
    assert not result.valid
    assert any(b.reason == "content_tampered" for b in result.breaks)


def test_editing_a_timestamp_is_detected():
    """Moving an event to claim it happened before the incident."""
    records, hashes = build_chain()
    records[3] = replace(records[3], occurred_at=NOW - timedelta(days=1))

    result = verify_chain(records, hashes)
    assert not result.valid
    assert any(b.reason == "content_tampered" and b.seq == 4 for b in result.breaks)


def test_deleting_an_event_is_detected():
    """Removing an inconvenient step from the middle of the record."""
    records, hashes = build_chain()
    del records[2]
    del hashes[2]

    result = verify_chain(records, hashes)
    assert not result.valid
    reasons = {b.reason for b in result.breaks}
    assert "sequence_gap" in reasons or "broken_link" in reasons


def test_truncating_the_chain_end_is_detected_by_head_hash():
    """Dropping the last events leaves a chain that is internally consistent.

    Verification alone cannot detect this, which is a real and worth-stating
    limitation: the head hash must be anchored somewhere outside the table -
    checkpointed to the object store or to an external log - for truncation to
    be detectable. This test documents the gap rather than pretending it away.
    """
    records, hashes = build_chain()
    anchored_head = hashes[-1]

    truncated = verify_chain(records[:3], hashes[:3])
    assert truncated.valid, "a truncated prefix is internally consistent"
    assert truncated.head_hash != anchored_head, "but the head no longer matches"


def test_reordering_events_is_detected():
    records, hashes = build_chain()
    records[1], records[2] = records[2], records[1]
    hashes[1], hashes[2] = hashes[2], hashes[1]

    result = verify_chain(records, hashes)
    assert not result.valid


def test_inserting_a_forged_event_is_detected():
    """Adding a decision that never happened."""
    records, hashes = build_chain()

    forged, forged_hash = next_link(
        seq=3,
        actor="attacker",
        actor_role="admin",
        action="approval_granted",
        subject_kind="incident",
        subject_id="INC-001",
        incident_id="INC-001",
        payload={"forged": True},
        occurred_at=NOW,
        prev_hash=hashes[1],
    )
    records.insert(2, forged)
    hashes.insert(2, forged_hash)

    result = verify_chain(records, hashes)
    assert not result.valid
    # The forged event links correctly, but everything after it now does not.
    assert any(b.reason == "broken_link" for b in result.breaks)


def test_an_unkeyed_chain_does_not_survive_a_full_rewrite():
    """The limit of a plain hash chain, stated honestly.

    An attacker who edits a row *and recomputes every hash after it* produces
    a chain that verifies cleanly. This test exists to document that, because
    the opposite is widely assumed and claiming it would be false assurance.

    The control that actually defeats this is keyed hashing, tested below: the
    HMAC key is held by the application and never by the database role, so an
    attacker with database write access cannot recompute anything.
    """
    records, hashes = build_chain()

    records[2] = replace(records[2], payload={"step": 2, "detail": "rewritten"})
    hashes[2] = compute_event_hash(records[2])

    for index in range(3, len(records)):
        records[index] = replace(records[index], prev_hash=hashes[index - 1])
        hashes[index] = compute_event_hash(records[index])

    result = verify_chain(records, hashes)
    assert result.valid, (
        "an unkeyed chain cannot detect a full rewrite; this is why the "
        "append-only grant and the HMAC key both exist"
    )


def test_a_keyed_chain_defeats_a_full_rewrite():
    """With an HMAC key the attacker cannot forge valid hashes at all.

    They can still edit rows - the database does not stop that on its own -
    but every hash they write is wrong, so the tampering is detected at the
    point it starts.
    """
    key = b"audit-key-held-by-the-application-not-the-database"
    records, hashes = build_chain(key=key)

    assert verify_chain(records, hashes, key).valid

    # An attacker without the key edits a row and recomputes the rest the only
    # way they can: unkeyed.
    records[2] = replace(records[2], payload={"forged": True})
    hashes[2] = compute_event_hash(records[2])
    for index in range(3, len(records)):
        records[index] = replace(records[index], prev_hash=hashes[index - 1])
        hashes[index] = compute_event_hash(records[index])

    result = verify_chain(records, hashes, key)
    assert not result.valid
    assert any(b.reason == "content_tampered" for b in result.breaks)


def test_a_keyed_chain_does_not_verify_with_the_wrong_key():
    key = b"the-real-key"
    records, hashes = build_chain(key=key)

    assert verify_chain(records, hashes, key).valid
    assert not verify_chain(records, hashes, b"a-guessed-key").valid
    assert not verify_chain(records, hashes, None).valid


def test_partial_tampering_is_detected_with_or_without_a_key():
    """The attacker who edits one row and stops is caught either way."""
    for key in (None, b"some-key"):
        records, hashes = build_chain(key=key)
        records[2] = replace(records[2], payload={"tampered": True})

        result = verify_chain(records, hashes, key)
        assert not result.valid
        assert result.breaks[0].seq == 3


def test_a_single_tampered_row_does_not_cascade():
    """One edited row reports one break, not one per subsequent event.

    Cascading would bury where the damage actually starts, which is the first
    thing an investigator needs to know.
    """
    records, hashes = build_chain(8)
    records[3] = replace(records[3], payload={"tampered": True})

    result = verify_chain(records, hashes)
    assert len(result.breaks) == 1
    assert result.breaks[0].seq == 4


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_verification_reports_where_the_break_is():
    records, hashes = build_chain()
    records[2] = replace(records[2], payload={"tampered": True})

    description = verify_chain(records, hashes).describe()
    assert "BROKEN" in description
    assert "seq 3" in description


def test_a_valid_chain_describes_itself_clearly():
    records, hashes = build_chain(4)
    assert "4 events intact" in verify_chain(records, hashes).describe()


def test_mismatched_inputs_are_rejected():
    records, hashes = build_chain(3)
    with pytest.raises(ValueError, match="cannot be verified"):
        verify_chain(records, hashes[:2])


# ---------------------------------------------------------------------------
# Content coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "different"),
        ("actor_role", "admin"),
        ("action", "different_action"),
        ("subject_kind", "shipment"),
        ("subject_id", "SH-9999"),
        ("incident_id", "INC-999"),
        ("seq", 99),
        ("prev_hash", "f" * 64),
    ],
)
def test_every_meaningful_field_is_covered_by_the_hash(field: str, value: object):
    """A field outside the hash is a field an attacker can edit freely."""
    record, digest = next_link(
        seq=1,
        actor="a",
        actor_role="dispatcher",
        action="x",
        subject_kind="incident",
        subject_id="INC-1",
        incident_id="INC-1",
        payload={"k": "v"},
        occurred_at=NOW,
        prev_hash=GENESIS_HASH,
    )
    assert compute_event_hash(replace(record, **{field: value})) != digest
