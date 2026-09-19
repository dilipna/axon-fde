"""Replaying the flagship scenario end to end, against real systems.

This is the test that turns B2's claims into something runnable. It touches
both databases and the recorded scenario at once, which is the point: the
ERP/BOL conflict is only meaningful if it comes out of the *seeded* ERP rather
than out of a fixture that agrees with itself.

Three things are established here:

1. Replaying the scenario persists evidence that survives a round trip.
2. The conflict between the ERP and the signed shipping document is detected
   from real seeded data, not asserted.
3. The baseline threshold alarm fires at minute 137 - the moment the recorded
   trajectory crosses the envelope - which is the number every future
   lead-time claim is measured against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit.service import AuditService
from backend.app.db.app.models import Evidence as EvidenceRow
from backend.app.db.app.repositories import (
    AuditRepository,
    EvidenceRepository,
    IncidentRepository,
)
from backend.app.db.legacy.repository import LegacyRepository
from backend.app.domain.enums import (
    DetectedBy,
    EvidenceRelation,
    EvidenceSource,
    IncidentSeverity,
    IncidentStatus,
)
from backend.app.evidence.reconciliation import reconcile
from backend.app.incidents.detection import BaselineDetector
from backend.app.incidents.lifecycle import IncidentService
from backend.app.incidents.replay import ScenarioReplay, resolve_envelope
from tests.conftest import recording
from tests.integration.conftest import TEST_HMAC_KEY

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRACTIONS = REPO_ROOT / "data" / "documents" / "extractions"

SHIPMENT = "SH-2041"
VEHICLE = "AX-042"
SCENARIO = "compressor_degradation_pharma_01"

#: The recorded trajectory crosses 8.0 C here. Locked by
#: `tests/unit/test_physics.py`, so a change to the thermal model cannot move
#: it silently.
EXPECTED_BREACH_MINUTE = 137


@pytest.fixture
def flagship() -> Path:
    """The generated flagship recording, or a skip that CI turns into a failure."""
    return recording(SCENARIO)


def make_replay(session: AsyncSession) -> ScenarioReplay:
    return ScenarioReplay(
        evidence=EvidenceRepository(session),
        incidents=IncidentRepository(session),
        audit=AuditService(session, key=TEST_HMAC_KEY),
        legacy=LegacyRepository(),
        detector=BaselineDetector(),
    )


# ---------------------------------------------------------------------------
# 1. The ERP/BOL conflict, from real seeded data
# ---------------------------------------------------------------------------


async def test_the_erp_holds_the_looser_envelope_than_the_signed_document(
    legacy_ready: None,
) -> None:
    """The precondition everything below depends on, read from the ERP itself.

    If the seed ever changed so that the ERP agreed with the bill of lading,
    the conflict tests would pass while proving nothing. This reads the real
    view and fails loudly instead.
    """
    requirement = await LegacyRepository().cargo_requirement(SHIPMENT)

    assert requirement is not None
    assert requirement.permitted_temp_max_c == 10.0, (
        "the ERP seed no longer holds 10.0 C for SH-2041; the conflict this "
        "scenario is built on has gone"
    )
    assert requirement.permitted_temp_min_c == 2.0


async def test_the_conflict_is_detected_between_the_erp_and_the_document(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """Two sources, one typed observation, a disagreement that surfaces.

    Nothing here knows that "the ERP" and "a bill of lading" are different
    kinds of thing. They produce the same observation type about the same
    shipment with different values, and reconciliation reports it. That is
    what makes this generalise beyond the one case.
    """
    replay = make_replay(app_session)
    context = await replay.load_context(
        shipment_id=SHIPMENT,
        vehicle_id=VEHICLE,
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        extraction_dir=EXTRACTIONS,
    )

    sources = {item.source for item in context}
    assert EvidenceSource.SQL_LEGACY in sources
    assert EvidenceSource.DOCUMENT_EXTRACTION in sources

    result = reconcile(context)
    conflicts = {conflict.observation_type for conflict in result.conflicts}
    assert "permitted_temp_max_c" in conflicts

    conflict = next(
        item for item in result.conflicts if item.observation_type == "permitted_temp_max_c"
    )
    assert conflict.safety_critical is True

    contradictions = [
        link for link in result.links if link.relation is EvidenceRelation.CONTRADICTS
    ]
    assert contradictions, "a detected conflict must produce a stored link"


async def test_a_shipment_whose_document_agrees_produces_no_conflict(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """The control. Without it, "a conflict was found" proves nothing.

    SH-2052's ERP row and its bill of lading both say 8 C. An adapter that
    reported a conflict for every shipment would pass the test above and fail
    this one.
    """
    replay = make_replay(app_session)
    context = await replay.load_context(
        shipment_id="SH-2052",
        vehicle_id="AX-051",
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        extraction_dir=EXTRACTIONS,
    )

    conflicts = {conflict.observation_type for conflict in reconcile(context).conflicts}
    assert "permitted_temp_max_c" not in conflicts


async def test_the_signed_document_wins_the_envelope(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """Authority order decides, and it decides for the stricter, safer limit.

    The taxonomy ranks `document_extraction` above `sql_legacy` for this type
    because the signed document is the legally operative one. Resolving to the
    ERP's 10 C instead would not produce a slightly different answer - it
    would produce silence where the alarm should be.
    """
    replay = make_replay(app_session)
    context = await replay.load_context(
        shipment_id=SHIPMENT,
        vehicle_id=VEHICLE,
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        extraction_dir=EXTRACTIONS,
    )

    envelope = resolve_envelope(context)
    assert envelope is not None
    assert envelope.maximum_c == 8.0
    assert envelope.minimum_c == 2.0


async def test_without_the_document_the_envelope_falls_back_to_the_erp(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """Degradation, not failure, when a source is missing.

    This is also the measurement of what the document is worth: the same
    shipment judged on the ERP alone is judged against 10 C, and the breach at
    minute 137 goes unreported.
    """
    replay = make_replay(app_session)
    context = await replay.load_context(
        shipment_id=SHIPMENT,
        vehicle_id=VEHICLE,
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        extraction_dir=None,
    )

    envelope = resolve_envelope(context)
    assert envelope is not None
    assert envelope.maximum_c == 10.0


async def test_an_unknown_shipment_yields_no_envelope_rather_than_a_default(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """Invariant I6: absence is represented as absence.

    Defaulting to a plausible envelope would mean judging cargo against an
    invented limit, and the resulting all-clear would look exactly like a real
    one.
    """
    replay = make_replay(app_session)
    context = await replay.load_context(
        shipment_id="SH-DOES-NOT-EXIST",
        vehicle_id="AX-999",
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        extraction_dir=EXTRACTIONS,
    )
    assert resolve_envelope(context) is None


# ---------------------------------------------------------------------------
# 2. The maintenance history is evidence too
# ---------------------------------------------------------------------------


async def test_the_prior_al17_warning_becomes_evidence(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """The vehicle's history is what makes today's fault code legible.

    AL17 observed now on a truck with no history is a novel event. On AX-042,
    which carries a prior AL17 warning, it is a known compressor degrading
    further - and that difference is what the diagnosis turns on.
    """
    replay = make_replay(app_session)
    context = await replay.load_context(
        shipment_id=SHIPMENT,
        vehicle_id=VEHICLE,
        observed_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
    )

    warnings = [item for item in context if item.observation_type == "maintenance_warning"]
    assert len(warnings) == 1
    assert "AL17" in warnings[0].value
    assert warnings[0].source is EvidenceSource.SQL_LEGACY


async def test_days_since_service_is_measured_against_the_observation_not_the_clock(
    legacy_ready: None,
    app_session: AsyncSession,
) -> None:
    """A replayed scenario must produce the same evidence every time.

    The ERP view computes `days_ago` against the server clock. Using it would
    make this observation drift by a day every day, and a stored benchmark
    result would stop reproducing.
    """
    replay = make_replay(app_session)
    observed_at = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)

    first = await replay.load_context(
        shipment_id=SHIPMENT, vehicle_id=VEHICLE, observed_at=observed_at
    )
    second = await replay.load_context(
        shipment_id=SHIPMENT, vehicle_id=VEHICLE, observed_at=observed_at
    )

    def days(context: list) -> float:  # type: ignore[type-arg]
        return next(
            item.value for item in context if item.observation_type == "days_since_last_service"
        )

    assert days(first) == days(second)
    # 2026-05-02 is the most recent service in the seed; 2026-07-14 is 73 days on.
    assert days(first) == pytest.approx(73.0, abs=0.1)


# ---------------------------------------------------------------------------
# 3. The full replay
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_replaying_the_flagship_scenario_persists_evidence_and_opens_one_incident(
    legacy_ready: None,
    app_session: AsyncSession,
    flagship: Path,
) -> None:
    """The headline: the recorded scenario runs through the whole pipeline.

    The baseline fires at minute 137, which is where the recorded trajectory
    crosses the 8 C envelope the signed document sets. That minute is the
    zero point for every lead-time claim this project will make, so it is
    asserted exactly rather than approximately.
    """
    replay = make_replay(app_session)
    result = await replay.run(
        flagship,
        shipment_id=SHIPMENT,
        scenario_run_id=SCENARIO,
        extraction_dir=EXTRACTIONS,
    )
    await app_session.commit()

    assert result.envelope is not None
    assert result.envelope.maximum_c == 8.0

    assert result.detected, "the baseline must fire on this scenario"
    assert result.detected_at_minute == EXPECTED_BREACH_MINUTE
    assert result.detection is not None
    assert result.detection.detected_by is DetectedBy.BASELINE
    assert result.detection.severity is IncidentSeverity.SEV3

    # The alarm is a notification, not a warning: it has nothing to say about
    # what happens next, which is exactly the gap B3 has to close.
    assert result.detection.predicted_breach_at is None

    stored = (
        await app_session.execute(sa.select(sa.func.count()).select_from(EvidenceRow))
    ).scalar_one()
    assert stored == result.evidence_persisted
    assert stored > result.readings_replayed, (
        "each reading produces several observations, so stored evidence must "
        "outnumber the readings replayed"
    )

    incidents = await IncidentRepository(app_session).open_incidents()
    assert len(incidents) == 1
    assert incidents[0].id == result.incident_id
    assert incidents[0].status == IncidentStatus.DETECTED.value
    assert incidents[0].detected_by == DetectedBy.BASELINE.value
    assert incidents[0].scenario_run_id == SCENARIO

    audit = AuditService(app_session, key=TEST_HMAC_KEY)
    verification = await audit.verify()
    assert verification.valid, verification.describe()


@pytest.mark.slow
async def test_the_persisted_evidence_round_trips_from_the_replay(
    legacy_ready: None,
    app_session: AsyncSession,
    flagship: Path,
) -> None:
    """Evidence written by a replay must come back as what it was.

    A replay that stored something unreadable would still pass a test that
    only counted rows.
    """
    replay = make_replay(app_session)
    result = await replay.run(
        flagship,
        shipment_id=SHIPMENT,
        scenario_run_id=SCENARIO,
        extraction_dir=EXTRACTIONS,
    )
    await app_session.commit()

    repository = EvidenceRepository(app_session)
    app_session.expunge_all()

    for original in result.context_evidence:
        restored = await repository.get(original.id)
        assert restored is not None, f"{original.observation_type} did not survive storage"
        assert restored == original

    temperatures = await repository.active_observations(
        entity_kind="vehicle", entity_id=VEHICLE, observation_type="cargo_temp_c"
    )
    assert temperatures
    assert all(item.source is EvidenceSource.TELEMETRY for item in temperatures)
    assert all(2.0 < float(item.value) < 40.0 for item in temperatures)


@pytest.mark.slow
async def test_the_audit_trail_records_the_conflict_and_the_detection(
    legacy_ready: None,
    app_session: AsyncSession,
    flagship: Path,
) -> None:
    """An auditor asks what was known and when. The chain has to answer.

    Both the conflict and the detection must be in the record: an incident
    whose audit trail does not mention that two sources disagreed about the
    envelope is one nobody can review.
    """
    replay = make_replay(app_session)
    result = await replay.run(
        flagship,
        shipment_id=SHIPMENT,
        scenario_run_id=SCENARIO,
        extraction_dir=EXTRACTIONS,
    )
    await app_session.commit()

    events = await AuditRepository(app_session).rows()
    actions = [event.action for event in events]
    assert "evidence.context_loaded" in actions
    assert "incident.detected" in actions

    context_event = next(event for event in events if event.action == "evidence.context_loaded")
    recorded = {entry["observation_type"] for entry in context_event.payload["conflicts"]}
    assert "permitted_temp_max_c" in recorded
    assert context_event.payload["envelope_max_c"] == 8.0

    detection_event = next(event for event in events if event.action == "incident.detected")
    assert detection_event.incident_id == result.incident_id
    assert detection_event.payload["detector"] == DetectedBy.BASELINE.value
    assert detection_event.payload["predicted_breach_at"] is None


@pytest.mark.slow
async def test_replaying_past_the_first_breach_does_not_open_a_second_incident(
    legacy_ready: None,
    app_session: AsyncSession,
    flagship: Path,
) -> None:
    """One degrading truck is one incident, not the hundred readings after it.

    Every minute from 137 onward breaches the envelope. Without deduplication
    this scenario alone would produce a hundred incidents and the dispatcher
    would stop reading them.
    """
    replay = make_replay(app_session)
    result = await replay.run(
        flagship,
        shipment_id=SHIPMENT,
        scenario_run_id=SCENARIO,
        extraction_dir=EXTRACTIONS,
        stop_at_first_detection=False,
    )
    await app_session.commit()

    assert result.readings_replayed == 240
    incidents = await IncidentRepository(app_session).open_incidents()
    assert len(incidents) == 1, (
        f"{len(incidents)} incidents opened for one degrading vehicle; deduplication is not working"
    )
    assert incidents[0].detected_at.astimezone(UTC) == datetime(2026, 7, 14, 12, 17, tzinfo=UTC), (
        "the incident must be dated to the first breach, not to the last reading"
    )


@pytest.mark.slow
async def test_a_detection_outside_the_suppression_window_opens_a_new_incident(
    legacy_ready: None,
    app_session: AsyncSession,
    flagship: Path,
) -> None:
    """Deduplication must not swallow a genuinely separate excursion.

    The more expensive failure of the two: a duplicate incident is noise, but
    a suppressed one is an excursion nobody was told about.
    """
    from datetime import timedelta

    replay = make_replay(app_session)
    result = await replay.run(
        flagship,
        shipment_id=SHIPMENT,
        scenario_run_id=SCENARIO,
        extraction_dir=EXTRACTIONS,
    )
    await app_session.commit()
    assert result.detection is not None

    lifecycle = IncidentService(
        IncidentRepository(app_session),
        EvidenceRepository(app_session),
        AuditService(app_session, key=TEST_HMAC_KEY),
    )

    inside = await lifecycle.record(replace_detected_at(result.detection, timedelta(hours=5)))
    assert inside.deduplicated

    outside = await lifecycle.record(replace_detected_at(result.detection, timedelta(hours=7)))
    assert outside.created
    await app_session.commit()

    assert outside.incident.id != inside.incident.id


def replace_detected_at(detection, offset):  # type: ignore[no-untyped-def]
    """The same detection, seen later. Used to cross the suppression window."""
    from dataclasses import replace

    return replace(detection, detected_at=detection.detected_at + offset)
