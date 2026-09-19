"""The application database, tested against a real PostgreSQL.

Three claims are made about this schema, and each is worth only as much as the
test behind it:

1. The migration builds the whole schema from nothing.
2. ``audit_event`` cannot be rewritten, and that holds at the database rather
   than only in application code.
3. Concurrent appends to the audit chain produce one chain, not a fork.

The third is the one that would otherwise be assumed rather than checked. A
single-threaded test of an append function passes whether or not the locking
is correct, so the test below genuinely appends at the same time, and a second
test shows what happens without the lock - because a concurrency test that
would pass on broken code is not a test.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.audit.chain import GENESIS_HASH
from backend.app.audit.service import AuditService
from backend.app.config import get_settings
from backend.app.db.app.models import AuditEvent, Base
from backend.app.db.app.repositories import (
    AuditRepository,
    EvidenceRepository,
    IncidentRepository,
)
from backend.app.db.app.session import build_engine
from backend.app.domain.enums import (
    DetectedBy,
    EvidenceRelation,
    EvidenceSource,
    EvidenceStatus,
    IncidentSeverity,
    IncidentStatus,
    Modality,
)
from backend.app.domain.evidence import EntityRef, Evidence, EvidenceLink, Provenance
from tests.integration.conftest import TEST_HMAC_KEY

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = "backend/app/db/migrations/alembic.ini"

#: The schema is documented as 12 tables in CONTINUE.md and the domain model.
#: Asserted rather than derived, so that adding a table is a deliberate act
#: that updates the documentation with it.
EXPECTED_TABLE_COUNT = 12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_evidence(
    *,
    observation_type: str = "cargo_temp_c",
    value: float | str | bool | list[str] = 4.0,
    entity_id: str = "AX-042",
    source: EvidenceSource = EvidenceSource.TELEMETRY,
    modality: Modality = Modality.TIMESERIES,
    observed_at: datetime | None = None,
) -> Evidence:
    """A valid observation built through the only supported construction path."""
    moment = observed_at or datetime(2026, 3, 14, 9, 30, tzinfo=UTC)
    return Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id=entity_id),
        source=source,
        modality=modality,
        observation_type=observation_type,
        value=value,
        observed_at=moment,
        ingested_at=moment + timedelta(seconds=30),
        provenance=Provenance(
            producer="test_app_db",
            producer_version="1.0.0",
            note="round-trip fixture",
        ),
    )


async def append_one(session: AsyncSession, marker: int, barrier: asyncio.Barrier) -> None:
    """Append a single event, starting at the same instant as its siblings.

    The barrier is what makes this a concurrency test. Without it, asyncio is
    free to run the tasks one after another and the test would pass against an
    unlocked implementation.
    """
    service = AuditService(session, key=TEST_HMAC_KEY)
    await barrier.wait()
    await service.append(
        actor=f"worker-{marker}",
        action="incident.detected",
        subject_kind="incident",
        subject_id=f"job-{marker}",
        payload={"marker": marker},
    )
    await session.commit()


def _swap_database(dsn: str, database: str) -> str:
    """Point a DSN at a different database on the same server."""
    return f"{dsn.rsplit('/', 1)[0]}/{database}"


# ---------------------------------------------------------------------------
# 1. The migration builds the schema
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_migration_builds_the_whole_schema_from_an_empty_database(
    app_db_ready: None,
) -> None:
    """`poe migrate` against an empty database produces a complete schema.

    Run against a throwaway database rather than the working one, so the claim
    is about migrating from nothing rather than about the state a developer's
    machine happens to be in. The real Alembic entry point is invoked as a
    subprocess, because that is what a deployment runs - calling the migration
    functions directly would skip env.py, where the DSN and the transaction
    handling live.
    """
    settings = get_settings()
    throwaway = f"axon_migrate_test_{uuid4().hex[:8]}"
    admin = build_engine(owner=True)

    async def manage(statement: str) -> None:
        # CREATE and DROP DATABASE cannot run inside a transaction block.
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(sa.text(statement))

    await manage(f'CREATE DATABASE "{throwaway}"')
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI, "upgrade", "head"],
            cwd=PROJECT_ROOT,
            env=dict(os.environ, POSTGRES_DB=throwaway),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, f"migration failed:\n{result.stdout}\n{result.stderr}"

        probe = build_engine(_swap_database(settings.postgres_dsn, throwaway))
        try:
            async with probe.connect() as connection:
                tables = set(
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = 'public'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                triggers = set(
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT tgname FROM pg_trigger "
                                "WHERE tgrelid = 'audit_event'::regclass "
                                "AND NOT tgisinternal"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                extensions = set(
                    (await connection.execute(sa.text("SELECT extname FROM pg_extension")))
                    .scalars()
                    .all()
                )
                grants = set(
                    (
                        await connection.execute(
                            sa.text(
                                "SELECT privilege_type FROM information_schema.table_privileges "
                                "WHERE table_name = 'audit_event' AND grantee = :role"
                            ),
                            {"role": settings.postgres_app_user},
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            await probe.dispose()

        missing = set(Base.metadata.tables) - tables
        assert not missing, f"migration did not create: {sorted(missing)}"
        assert len(Base.metadata.tables) == EXPECTED_TABLE_COUNT
        assert "trg_audit_event_append_only" in triggers
        # The pgvector image ships the extension but does not enable it, and
        # the Phase 4 retrieval path assumes it is already there.
        assert "vector" in extensions
        # The append-only grants must exist in a database built from scratch,
        # not only in one a developer patched by hand.
        assert grants == {"SELECT", "INSERT"}
    finally:
        await manage(f'DROP DATABASE IF EXISTS "{throwaway}" WITH (FORCE)')
        await admin.dispose()


async def test_the_application_connects_as_a_role_that_cannot_rewrite_history(
    app_session: AsyncSession,
) -> None:
    """The grants protect the audit log only if the app actually uses that role.

    Two separate things are checked because either one alone can be true while
    the protection is worthless: the role named in Settings holds exactly
    SELECT and INSERT on `audit_event`, *and* that is the role the application
    is connected as. Configuring a restricted role and then connecting as the
    owner is the obvious way for this to silently regress.
    """
    settings = get_settings()

    connected_as = (await app_session.execute(sa.text("SELECT current_user"))).scalar_one()
    assert connected_as == settings.postgres_app_user

    privileges = set(
        (
            await app_session.execute(
                sa.text(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE table_name = 'audit_event' AND grantee = :role"
                ),
                {"role": settings.postgres_app_user},
            )
        )
        .scalars()
        .all()
    )
    assert privileges == {"SELECT", "INSERT"}

    is_superuser = (
        await app_session.execute(
            sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        )
    ).scalar_one()
    # A superuser bypasses privilege checks entirely, which would make the
    # assertion above true and meaningless at the same time.
    assert is_superuser is False


# ---------------------------------------------------------------------------
# 2. The audit table cannot be rewritten
# ---------------------------------------------------------------------------


async def test_the_runtime_role_can_append_to_the_audit_log(app_session: AsyncSession) -> None:
    """The baseline the next four tests depend on: appending works."""
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    event = await service.append(
        actor="detector",
        action="incident.detected",
        subject_kind="incident",
        subject_id="INC-1",
    )
    await app_session.commit()

    assert event.seq == 1
    assert event.prev_hash == GENESIS_HASH


async def test_updating_an_audit_event_leaves_the_record_unchanged(
    app_session: AsyncSession,
) -> None:
    """An attacker with the application's own credentials cannot edit history.

    Asserted as an outcome rather than as an exception type. What matters is
    not which error the driver raises but that the row on disk still says what
    it said before, which is the claim a compliance auditor relies on.
    """
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    await service.append(
        actor="dispatcher-7",
        action="approval.granted",
        subject_kind="approval",
        subject_id="APR-1",
    )
    await app_session.commit()

    with pytest.raises(DBAPIError):
        await app_session.execute(sa.text("UPDATE audit_event SET actor = 'somebody-else'"))
    await app_session.rollback()

    surviving = (await app_session.execute(sa.select(AuditEvent.actor))).scalars().all()
    assert list(surviving) == ["dispatcher-7"]


async def test_deleting_an_audit_event_leaves_the_record_in_place(
    app_session: AsyncSession,
) -> None:
    """Truncating history is the cheapest way to hide a bad decision."""
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    await service.append(
        actor="executor",
        action="action.executed",
        subject_kind="execution",
        subject_id="EXE-1",
    )
    await app_session.commit()

    with pytest.raises(DBAPIError):
        await app_session.execute(sa.text("DELETE FROM audit_event"))
    await app_session.rollback()

    assert await AuditRepository(app_session).count() == 1


async def test_truncate_is_denied_to_the_runtime_role(app_session: AsyncSession) -> None:
    """TRUNCATE does not fire an UPDATE/DELETE trigger.

    Without an explicit revoke this would be a single statement that walks
    straight through both database layers - the kind of hole that only shows
    up when someone goes looking for it.
    """
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    await service.append(
        actor="detector",
        action="incident.detected",
        subject_kind="incident",
        subject_id="INC-2",
    )
    await app_session.commit()

    with pytest.raises(DBAPIError):
        await app_session.execute(sa.text("TRUNCATE audit_event"))
    await app_session.rollback()

    assert await AuditRepository(app_session).count() == 1


async def test_the_trigger_stops_even_a_connection_that_owns_the_table(
    app_session: AsyncSession,
    owner_session: AsyncSession,
) -> None:
    """Grants protect nothing against a superuser; the trigger still does.

    This is the test that justifies having two database roles at all. The
    owner is a superuser, so PostgreSQL skips its privilege checks entirely.
    Without this test, one of the two layers could be quietly doing nothing
    and the suite would still be green.
    """
    await AuditService(app_session, key=TEST_HMAC_KEY).append(
        actor="dispatcher-7",
        action="approval.granted",
        subject_kind="approval",
        subject_id="APR-2",
    )
    await app_session.commit()

    with pytest.raises(DBAPIError):
        await owner_session.execute(sa.text("UPDATE audit_event SET actor = 'rewritten'"))
    await owner_session.rollback()

    surviving = (await owner_session.execute(sa.select(AuditEvent.actor))).scalars().all()
    assert list(surviving) == ["dispatcher-7"]


# ---------------------------------------------------------------------------
# 3. Concurrent appends produce one chain
# ---------------------------------------------------------------------------

#: Enough writers to lose a race reliably, few enough that a 1 GB Postgres
#: container is comfortable holding a connection open for each of them.
CONCURRENT_APPENDERS = 12


@pytest_asyncio.fixture
async def concurrent_engine(owner_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """An engine with a connection for every writer in the tests below.

    Sized deliberately rather than inherited from the application defaults.
    These tests synchronise on a barrier that every writer must reach, and
    each writer needs a connection to get there; with the application's pool
    of ten, the last two writers block waiting for a connection while the
    other ten block waiting for them, and the test hangs rather than failing.

    That is a property of the test, not of the append path - production
    writers queue on the pool and proceed - but it is the kind of deadlock
    that reads as a database fault when it appears.
    """
    settings = get_settings().model_copy(
        update={
            "postgres_pool_size": CONCURRENT_APPENDERS,
            "postgres_max_overflow": 2,
        }
    )
    engine = build_engine(settings=settings)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_concurrent_appends_produce_a_single_valid_chain(
    concurrent_engine: AsyncEngine,
) -> None:
    """Twelve simultaneous appends yield one chain, numbered 1..12, intact.

    Every appender gets its own session and therefore its own connection and
    transaction; sharing a session would serialise them in the client and
    prove nothing about the database.
    """
    maker = async_sessionmaker(concurrent_engine, expire_on_commit=False)
    sessions = [maker() for _ in range(CONCURRENT_APPENDERS)]
    barrier = asyncio.Barrier(CONCURRENT_APPENDERS)

    # Exceptions are collected rather than raised, so that a collision is
    # reported as the collision it is. Letting one propagate here tears down
    # the remaining sessions mid-transaction, and the resulting SQLAlchemy
    # state error is what the reader sees instead of the real cause.
    results = await asyncio.gather(
        *(append_one(session, index, barrier) for index, session in enumerate(sessions)),
        return_exceptions=True,
    )
    for session in sessions:
        with suppress(Exception):
            await session.close()

    failures = [item for item in results if isinstance(item, BaseException)]
    assert not failures, (
        f"{len(failures)} of {CONCURRENT_APPENDERS} concurrent appends failed. "
        f"The first was {failures[0]!r} - two writers claimed the same sequence "
        "number, which means they read the same head."
    )

    async with maker() as reader:
        verification = await AuditService(reader, key=TEST_HMAC_KEY).verify()
        sequences = list(
            (await reader.execute(sa.select(AuditEvent.seq).order_by(AuditEvent.seq)))
            .scalars()
            .all()
        )
        actors = list((await reader.execute(sa.select(AuditEvent.actor))).scalars().all())

    assert sequences == list(range(1, CONCURRENT_APPENDERS + 1)), (
        "sequence numbers must be contiguous with no gaps and no duplicates"
    )
    assert verification.valid, verification.describe()
    assert verification.events_checked == CONCURRENT_APPENDERS
    # Every writer's event survived. A fork that the unique constraint caught
    # would show up here as a lost append rather than as a broken chain.
    assert sorted(actors) == sorted(f"worker-{i}" for i in range(CONCURRENT_APPENDERS))


async def test_appending_without_the_lock_does_not_produce_a_valid_chain(
    concurrent_engine: AsyncEngine,
) -> None:
    """The lock is load-bearing, shown by removing it.

    This is the control for the test above. It reimplements the append the
    naive way - read the head, then insert - and shows that concurrent writers
    do not end up with an intact chain. Without this, a passing concurrency
    test would be equally consistent with the lock doing nothing at all.

    The assertion is deliberately loose about *how* it fails. Under the unique
    constraint on `seq` the losing writers are rejected outright; relax that
    constraint and the same race would instead be a genuine fork. Either is a
    failure, and pinning the test to one of them would make it fragile in the
    direction of being wrong rather than noisy.
    """
    maker = async_sessionmaker(concurrent_engine, expire_on_commit=False)
    sessions = [maker() for _ in range(CONCURRENT_APPENDERS)]
    barrier = asyncio.Barrier(CONCURRENT_APPENDERS)

    async def naive_append(session: AsyncSession, marker: int) -> None:
        repository = AuditRepository(session)
        last_seq, head_hash = await repository.head()  # no lock_chain() call
        await barrier.wait()  # every writer now holds the same stale head
        session.add(
            AuditEvent(
                seq=last_seq + 1,
                prev_hash=head_hash,
                event_hash=f"{marker:064d}",
                actor=f"worker-{marker}",
                action="incident.detected",
                subject_kind="incident",
                subject_id=f"job-{marker}",
                payload={},
                occurred_at=datetime.now(UTC),
            )
        )
        await session.commit()

    results = await asyncio.gather(
        *(naive_append(session, index) for index, session in enumerate(sessions)),
        return_exceptions=True,
    )
    for session in sessions:
        with suppress(Exception):
            await session.close()

    async with maker() as reader:
        stored = await AuditRepository(reader).count()

    failures = [item for item in results if isinstance(item, BaseException)]
    assert not (stored == CONCURRENT_APPENDERS and not failures), (
        "unlocked concurrent appends produced a complete chain, which means "
        "the writers did not actually overlap and the locked test above is "
        "not proving anything"
    )


async def test_an_append_reads_the_head_written_by_the_previous_one(
    app_session: AsyncSession,
) -> None:
    """Sequential appends link to each other, which is the baseline property."""
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    first = await service.append(
        actor="a", action="incident.detected", subject_kind="incident", subject_id="INC-3"
    )
    second = await service.append(
        actor="b", action="incident.triaged", subject_kind="incident", subject_id="INC-3"
    )
    await app_session.commit()

    assert second.seq == first.seq + 1
    assert second.prev_hash == first.event_hash
    assert (await service.verify()).valid


async def test_a_keyed_chain_does_not_verify_under_the_wrong_key(
    app_session: AsyncSession,
) -> None:
    """The HMAC key is what defeats a rewrite, so it has to actually matter.

    If verification passed under any key, the keying would be decoration and
    the claim in `chain.py` about surviving a full rewrite would be false.
    """
    await AuditService(app_session, key=TEST_HMAC_KEY).append(
        actor="a", action="incident.detected", subject_kind="incident", subject_id="INC-4"
    )
    await app_session.commit()

    assert (await AuditService(app_session, key=TEST_HMAC_KEY).verify()).valid
    other = await AuditService(app_session, key=b"a-different-key").verify()
    assert not other.valid
    assert other.breaks[0].reason == "content_tampered"


async def test_an_audit_append_rolls_back_with_the_work_it_describes(
    app_session: AsyncSession,
) -> None:
    """An audit event must not outlive the action it claims happened.

    Committing the audit write separately would be the obvious way to make
    sure nothing is ever missed, and it would produce a record asserting that
    an action occurred when the transaction that would have performed it was
    rolled back.
    """
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    await service.append(
        actor="executor",
        action="action.executed",
        subject_kind="execution",
        subject_id="EXE-9",
    )
    await app_session.rollback()

    assert await AuditRepository(app_session).count() == 0


async def test_a_naive_timestamp_is_refused_by_the_audit_service(
    app_session: AsyncSession,
) -> None:
    """A naive `occurred_at` hashes differently depending on the server clock.

    The chain would then verify on the machine that wrote it and fail
    everywhere else, which is the worst possible way for this to break.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        await AuditService(app_session, key=TEST_HMAC_KEY).append(
            actor="a",
            action="incident.detected",
            subject_kind="incident",
            subject_id="INC-5",
            occurred_at=datetime(2026, 3, 14, 9, 30),  # noqa: DTZ001
        )
    await app_session.rollback()


def test_the_chain_lock_key_is_stable_across_processes() -> None:
    """A per-process lock key is the same as having no lock at all.

    Python's built-in `hash()` is salted per interpreter, so deriving the key
    that way would give each worker its own lock - and the concurrency test
    above would still pass, because pytest runs in one process.
    """
    from backend.app.db.app.repositories.audit import CHAIN_LOCK_KEY

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend.app.db.app.repositories.audit import CHAIN_LOCK_KEY;"
            "print(CHAIN_LOCK_KEY)",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == CHAIN_LOCK_KEY


# ---------------------------------------------------------------------------
# 4. Evidence round-trips
# ---------------------------------------------------------------------------


async def test_evidence_round_trips_with_value_provenance_and_confidence_intact(
    app_session: AsyncSession,
) -> None:
    """Nothing a decision depends on may be lost in storage.

    Confidence in particular: an approval is bound to the evidence it was
    granted against, so a confidence that changed on reload would silently
    invalidate approvals or, worse, silently fail to.
    """
    original = make_evidence(value=4.5)
    repository = EvidenceRepository(app_session)
    await repository.add(original)
    await app_session.commit()

    app_session.expunge_all()
    restored = await repository.get(original.id)

    assert restored is not None
    assert restored.value == 4.5
    assert restored.confidence == original.confidence
    assert restored.confidence_basis is original.confidence_basis
    assert restored.freshness is original.freshness
    assert restored.provenance == original.provenance
    assert restored.entity_ref == original.entity_ref
    assert restored.content_hash == original.content_hash
    assert restored.observed_at == original.observed_at
    # The whole object, not only the fields named above: a field added later
    # must round-trip too, and this is what will catch it if it does not.
    assert restored == original


async def test_a_set_valued_observation_round_trips_as_a_list_of_strings(
    app_session: AsyncSession,
) -> None:
    """Fault codes are the multimodal case, so their shape must survive.

    A JSONB array that came back as a string, or with its members reordered,
    would change `content_hash` and silently invalidate any approval bound to
    it. The domain model sorts before hashing precisely because order is not
    meaningful; storage must not reorder the stored value itself.
    """
    original = make_evidence(
        observation_type="maintenance_warning",
        value=["AL17", "AL02"],
        source=EvidenceSource.SQL_LEGACY,
        modality=Modality.STRUCTURED,
    )
    repository = EvidenceRepository(app_session)
    await repository.add(original)
    await app_session.commit()

    app_session.expunge_all()
    restored = await repository.get(original.id)

    assert restored is not None
    assert restored.value == ["AL17", "AL02"]
    assert restored.content_hash == original.content_hash


async def test_a_categorical_observation_round_trips_as_its_declared_value(
    app_session: AsyncSession,
) -> None:
    """A categorical value outside its allowed set cannot re-enter the domain."""
    original = make_evidence(observation_type="door_state", value="ajar")
    repository = EvidenceRepository(app_session)
    await repository.add(original)
    await app_session.commit()

    app_session.expunge_all()
    restored = await repository.get(original.id)

    assert restored is not None
    assert restored.value == "ajar"
    assert isinstance(restored.value, str)


async def test_evidence_links_are_queryable_from_either_end(
    app_session: AsyncSession,
) -> None:
    """A conflict is a fact about both observations, not just the newer one.

    This is the flagship scenario's ERP-versus-bill-of-lading disagreement:
    the ERP says the cargo may go to 10 C, the shipping document says 8 C.
    """
    repository = EvidenceRepository(app_session)
    erp = await repository.add(
        make_evidence(
            observation_type="permitted_temp_max_c",
            value=10.0,
            source=EvidenceSource.SQL_LEGACY,
            modality=Modality.STRUCTURED,
        )
    )
    bol = await repository.add(
        make_evidence(
            observation_type="permitted_temp_max_c",
            value=8.0,
            source=EvidenceSource.DOCUMENT_EXTRACTION,
            modality=Modality.TEXT,
        )
    )
    await repository.add_link(
        EvidenceLink(
            from_evidence_id=erp.id,
            to_evidence_id=bol.id,
            relation=EvidenceRelation.CONTRADICTS,
            rule_id="numeric_tolerance",
            detail="ERP says 10.0 C, bill of lading says 8.0 C",
        )
    )
    await app_session.commit()

    from_erp = await repository.links_for(erp.id)
    from_bol = await repository.links_for(bol.id)

    assert len(from_erp) == 1
    assert from_erp == from_bol
    assert from_erp[0].relation is EvidenceRelation.CONTRADICTS


async def test_superseding_evidence_keeps_the_original_reading(
    app_session: AsyncSession,
) -> None:
    """History is what was known at the time, not what is true now.

    An audit of a decision needs the row that was acted on. Overwriting the
    value in place would make every past decision look as though it had been
    made on today's data.
    """
    repository = EvidenceRepository(app_session)
    old = await repository.add(make_evidence(value=4.0))
    await repository.add(
        make_evidence(value=6.5, observed_at=datetime(2026, 3, 14, 9, 35, tzinfo=UTC))
    )
    await app_session.commit()

    await repository.mark_status(old.id, EvidenceStatus.SUPERSEDED)
    await app_session.commit()

    app_session.expunge_all()
    restored = await repository.get(old.id)
    assert restored is not None
    assert restored.value == 4.0
    assert restored.status is EvidenceStatus.SUPERSEDED

    active = await repository.active_observations(
        entity_kind="vehicle", entity_id="AX-042", observation_type="cargo_temp_c"
    )
    assert [item.value for item in active] == [6.5]


async def test_an_llm_authored_observation_is_rejected_by_the_database(
    owner_session: AsyncSession,
) -> None:
    """Invariant I1, at the last layer that could still catch it.

    The enum makes this unrepresentable in Python, so the only way to reach
    the database CHECK is to bypass the ORM entirely - which is also the only
    way an attacker or a careless future migration would reach it.
    """
    with pytest.raises(DBAPIError) as caught:
        await owner_session.execute(
            sa.text(
                "INSERT INTO evidence (id, entity_kind, entity_id, source, modality, "
                "observation_type, value, observed_at, valid_from, ingested_at, "
                "confidence, confidence_basis, freshness, provenance, content_hash, status) "
                "VALUES (gen_random_uuid(), 'vehicle', 'AX-042', 'llm_inference', 'text', "
                "'cargo_temp_c', '{\"value\": 4.0}'::jsonb, now(), now(), now(), "
                "0.9, 'source_reliability', 'fresh', '{}'::jsonb, 'abc', 'active')"
            )
        )
    await owner_session.rollback()
    assert "ck_evidence_source_never_llm" in str(caught.value)


# ---------------------------------------------------------------------------
# 5. Incidents
# ---------------------------------------------------------------------------


async def test_an_incident_round_trips_and_carries_its_detector(
    app_session: AsyncSession,
) -> None:
    """`detected_by` is what makes lead time computable after the fact."""
    repository = IncidentRepository(app_session)
    incident = await repository.create(
        correlation_key="vehicle:AX-042:thermal",
        incident_type="thermal_excursion_risk",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-042",
        detected_by=DetectedBy.AXON,
        detected_at=datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
        predicted_breach_at=datetime(2026, 3, 14, 11, 47, tzinfo=UTC),
        scenario_run_id="compressor_degradation_pharma_01",
    )
    await app_session.commit()

    restored = await repository.get(incident.id)
    assert restored is not None
    assert restored.detected_by == DetectedBy.AXON.value
    assert restored.status == IncidentStatus.DETECTED.value
    assert restored.scenario_run_id == "compressor_degradation_pharma_01"


async def test_a_resolved_incident_is_no_longer_found_for_deduplication(
    app_session: AsyncSession,
) -> None:
    """Deduplication must attach to live incidents only.

    Attaching a new detection to a closed incident would suppress a real,
    separate excursion - a missed incident rather than a duplicate one, which
    is the more expensive of the two failures.
    """
    repository = IncidentRepository(app_session)
    key = "vehicle:AX-042:thermal"
    first = await repository.create(
        correlation_key=key,
        incident_type="thermal_excursion_risk",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-042",
        detected_by=DetectedBy.AXON,
        detected_at=datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
    )
    await app_session.commit()

    assert (await repository.active_for_correlation_key(key)) is not None

    await repository.set_status(
        first.id, IncidentStatus.RESOLVED, closed_at=datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
    )
    await app_session.commit()

    assert (await repository.active_for_correlation_key(key)) is None


async def test_a_superseded_incident_records_what_absorbed_it(
    app_session: AsyncSession,
) -> None:
    """The evidence attached to an absorbed incident must stay reachable."""
    repository = IncidentRepository(app_session)
    common = {
        "correlation_key": "vehicle:AX-042:thermal",
        "incident_type": "thermal_excursion_risk",
        "severity": IncidentSeverity.SEV2,
        "entity_kind": "vehicle",
        "entity_id": "AX-042",
        "detected_by": DetectedBy.BASELINE,
    }
    absorbed = await repository.create(
        detected_at=datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
        **common,  # type: ignore[arg-type]
    )
    surviving = await repository.create(
        detected_at=datetime(2026, 3, 14, 9, 35, tzinfo=UTC),
        **common,  # type: ignore[arg-type]
    )
    await app_session.commit()

    await repository.supersede(
        absorbed.id, by=surviving.id, at=datetime(2026, 3, 14, 9, 35, tzinfo=UTC)
    )
    await app_session.commit()

    restored = await repository.get(absorbed.id)
    assert restored is not None
    assert restored.status == IncidentStatus.SUPERSEDED.value
    assert restored.superseded_by == surviving.id
    assert [item.id for item in await repository.open_incidents()] == [surviving.id]


async def test_a_naive_detection_timestamp_is_refused(app_session: AsyncSession) -> None:
    """Lead time is measured from `detected_at`.

    A naive timestamp would make the headline number depend on the timezone of
    whichever machine happened to write the row.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        await IncidentRepository(app_session).create(
            correlation_key="vehicle:AX-042:thermal",
            incident_type="thermal_excursion_risk",
            severity=IncidentSeverity.SEV2,
            entity_kind="vehicle",
            entity_id="AX-042",
            detected_by=DetectedBy.AXON,
            detected_at=datetime(2026, 3, 14, 9, 30),  # noqa: DTZ001
        )


async def test_evidence_and_its_incident_commit_together(
    app_session: AsyncSession,
) -> None:
    """Repositories share the caller's transaction and do not commit alone.

    If each repository committed for itself, a failure between two of them
    would leave an incident with no evidence behind it, or evidence attached
    to an incident that was never opened.
    """
    incidents = IncidentRepository(app_session)
    evidence = EvidenceRepository(app_session)

    incident = await incidents.create(
        correlation_key="vehicle:AX-042:thermal",
        incident_type="thermal_excursion_risk",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-042",
        detected_by=DetectedBy.AXON,
        detected_at=datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
    )
    reading = make_evidence(value=6.0)
    await evidence.add(reading.model_copy(update={"incident_id": incident.id}))
    await app_session.rollback()

    assert await incidents.get(incident.id) is None
    assert await evidence.get(reading.id) is None


async def test_the_audit_events_for_one_incident_are_retrievable_on_their_own(
    app_session: AsyncSession,
) -> None:
    """An auditor asks about one shipment, not about the whole chain.

    The incident id also has to survive the hash round-trip: it is hashed as a
    string and stored as a UUID, and a mismatch between those two would make
    every chain containing an incident-scoped event fail to verify, but only
    after a reload.
    """
    incidents = IncidentRepository(app_session)
    incident = await incidents.create(
        correlation_key="vehicle:AX-042:thermal",
        incident_type="thermal_excursion_risk",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-042",
        detected_by=DetectedBy.AXON,
        detected_at=datetime(2026, 3, 14, 9, 30, tzinfo=UTC),
    )
    service = AuditService(app_session, key=TEST_HMAC_KEY)
    await service.append(
        actor="detector",
        action="incident.detected",
        subject_kind="incident",
        subject_id=str(incident.id),
        incident_id=incident.id,
        payload={"predicted_breach_at": "2026-03-14T11:47:00+00:00"},
    )
    await service.append(
        actor="detector",
        action="evidence.recorded",
        subject_kind="evidence",
        subject_id=str(uuid4()),
    )
    await app_session.commit()

    app_session.expunge_all()
    scoped = await AuditRepository(app_session).rows(incident_id=incident.id)
    assert [event.action for event in scoped] == ["incident.detected"]

    verification = await service.verify()
    assert verification.valid, verification.describe()
