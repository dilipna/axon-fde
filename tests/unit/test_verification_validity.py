"""Invalid or repeated evidence cannot authorize an incident resolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from backend.app.actions.effects import EffectKind, ExpectedEffect
from backend.app.db.app.models import Incident, OutcomeVerification
from backend.app.domain.enums import EvidenceSource, IncidentStatus, Modality
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.verification.outcome import Verdict, evaluate_effect
from backend.app.verification.service import VerificationService

START = datetime(2026, 10, 20, tzinfo=UTC)
END = START + timedelta(minutes=90)
ENVELOPE = TemperatureEnvelope(2.0, 8.0)
KINDS = [EffectKind.TEMPERATURE_WITHIN_ENVELOPE, EffectKind.TEMPERATURE_STOPS_RISING]


def grade(kind, readings, *, envelope=ENVELOPE, start=START, end=END):
    return evaluate_effect(
        ExpectedEffect(kind=kind, within_minutes=90),
        readings=readings,
        envelope=envelope,
        window_start=start,
        window_end=end,
    )


def trace(value=5.0):
    return [(START + timedelta(minutes=i), value) for i in range(91)]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True])
def test_invalid_reading_abstains_even_among_valid_evidence(kind, bad):
    readings = trace()
    readings[45] = (readings[45][0], bad)
    result = grade(kind, readings)
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.requires_follow_up
    json.dumps(result.observed, allow_nan=False)


@pytest.mark.parametrize("kind", KINDS)
def test_duplicate_delivery_does_not_change_a_valid_result(kind):
    readings = trace()
    original = grade(kind, readings)
    repeated = grade(kind, list(reversed(readings + readings)))
    assert repeated.verdict is original.verdict is Verdict.CONFIRMED
    assert repeated.observed["reading_count"] == len(readings)
    assert repeated.observed["duplicate_reading_count"] == len(readings)


def test_three_copies_of_one_reading_do_not_establish_recovery():
    result = grade(KINDS[0], [(START, 5.0)] * 3)
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.observed["reading_count"] == 1


def test_duplicates_cannot_meet_the_slope_sample_floor():
    result = grade(KINDS[1], [(START, 5.0)] * 5 + [(END, 5.0)] * 5)
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.observed["reading_count"] == 2


@pytest.mark.parametrize("kind", KINDS)
def test_conflicting_same_time_values_abstain_regardless_of_order(kind):
    readings = [*trace(), (START + timedelta(minutes=45), 10.0)]
    a = grade(kind, readings)
    b = grade(kind, list(reversed(readings)))
    assert a == b
    assert a.verdict is Verdict.INCONCLUSIVE
    assert a.observed["conflicting_timestamp_count"] == 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "envelope", [TemperatureEnvelope(float("nan"), 8.0), TemperatureEnvelope(8.0, 2.0)]
)
def test_invalid_envelope_cannot_authorize_resolution(kind, envelope):
    assert grade(kind, trace(), envelope=envelope).verdict is Verdict.INCONCLUSIVE


@pytest.mark.parametrize("kind", KINDS)
def test_reversed_window_abstains(kind):
    assert grade(kind, trace(), start=END, end=START).verdict is Verdict.INCONCLUSIVE


@pytest.mark.parametrize("kind", KINDS)
def test_naive_reading_timestamp_abstains_instead_of_raising(kind):
    readings = [*trace(), (START.replace(tzinfo=None), 5.0)]
    assert grade(kind, readings).verdict is Verdict.INCONCLUSIVE


def test_overflow_in_finite_slope_inputs_is_not_confirmation():
    readings = [(t, 1e308 if i % 2 else -1e308) for i, (t, _) in enumerate(trace())]
    result = grade(KINDS[1], readings)
    assert result.verdict is Verdict.INCONCLUSIVE
    json.dumps(result.observed, allow_nan=False)


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_value_outside_the_claim_window_does_not_poison_it(kind):
    readings = [*trace(), (START - timedelta(minutes=1), float("nan"))]
    assert grade(kind, readings).verdict is Verdict.CONFIRMED


def test_real_warming_still_fails_and_missing_data_still_abstains():
    warming = [(t, 6.0 + i * 0.023) for i, (t, _) in enumerate(trace())]
    assert grade(KINDS[1], warming).verdict is Verdict.FAILED
    assert grade(KINDS[0], []).verdict is Verdict.INCONCLUSIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [[5.0, 5.0, 5.0], [5.0, 6.0, 5.0]])
async def test_service_does_not_resolve_on_repeated_or_conflicting_evidence(values):
    """Real normalization and grading; persistence collaborators are test doubles."""
    from uuid import uuid4

    incident = Incident(id=uuid4(), status=IncidentStatus.VERIFYING.value)
    verification = OutcomeVerification(
        id=uuid4(),
        incident_id=incident.id,
        execution_id=uuid4(),
        expected_effect=ExpectedEffect(kind=KINDS[0], within_minutes=90).as_payload(),
        window_start=START,
        window_end=END,
    )
    evidence = [
        Evidence.create(
            entity_ref=EntityRef(kind="shipment", id="synthetic"),
            source=EvidenceSource.TELEMETRY,
            modality=Modality.STRUCTURED,
            observation_type="cargo_temp_c",
            value=value,
            observed_at=START,
            ingested_at=END,
            provenance=Provenance(producer="validity-regression"),
        )
        for value in values
    ]
    session, audit, incidents = AsyncMock(), AsyncMock(), AsyncMock()
    service = VerificationService(session, audit, incidents)
    result = await service.verify(
        verification, incident=incident, evidence=evidence, envelope=ENVELOPE, at=END
    )
    assert result.result.verdict is Verdict.INCONCLUSIVE
    assert result.incident_status is IncidentStatus.VERIFYING
    assert verification.follow_up_required
    incidents.transition.assert_not_awaited()
    audit.append.assert_awaited_once()
