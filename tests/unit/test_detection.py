"""The baseline detector and the incident state machine.

The baseline is the number every lead-time claim is measured against, so
these tests care as much about it *not* being better than a threshold alarm
as about it working. A baseline that quietly smoothed its input or waited for
confirmation would make the comparison flattering and meaningless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.domain.enums import (
    DetectedBy,
    EvidenceSource,
    EvidenceStatus,
    IncidentSeverity,
    IncidentStatus,
    Modality,
)
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.incidents.detection import (
    BaselineDetector,
    TemperatureEnvelope,
    correlation_key,
)
from backend.app.incidents.lifecycle import (
    TRANSITIONS,
    IllegalTransitionError,
    is_legal_transition,
)

pytestmark = pytest.mark.unit

START = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
PHARMA = TemperatureEnvelope(minimum_c=2.0, maximum_c=8.0)


def reading(
    temperature: float,
    *,
    minute: int = 0,
    entity_id: str = "AX-042",
    status: EvidenceStatus = EvidenceStatus.ACTIVE,
) -> Evidence:
    moment = START + timedelta(minutes=minute)
    evidence = Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id=entity_id),
        source=EvidenceSource.TELEMETRY,
        modality=Modality.TIMESERIES,
        observation_type="cargo_temp_c",
        value=temperature,
        observed_at=moment,
        ingested_at=moment,
        provenance=Provenance(producer="test_detection"),
    )
    if status is not EvidenceStatus.ACTIVE:
        evidence = evidence.model_copy(update={"status": status})
    return evidence


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("temperature", "breaches"),
    [
        (2.0, False),  # exactly at the lower limit is in spec
        (8.0, False),  # exactly at the upper limit is in spec
        (1.99, True),
        (8.01, True),
        (5.0, False),
    ],
)
def test_the_envelope_boundary_is_inclusive(temperature: float, breaches: bool) -> None:
    """A contractual limit of 8 C means 8 C is permitted.

    Off by one in the exclusive direction would raise an incident for cargo
    that is exactly in spec, and the customer's own alarm would disagree with
    ours on the boundary case most likely to come up in a dispute.
    """
    assert PHARMA.breaches(temperature) is breaches


# ---------------------------------------------------------------------------
# The baseline detector
# ---------------------------------------------------------------------------


def test_the_baseline_stays_silent_while_cargo_is_in_spec() -> None:
    detector = BaselineDetector()
    assert detector.evaluate([reading(5.0)], envelope=PHARMA, now=START) is None


def test_the_baseline_fires_on_a_single_breaching_reading() -> None:
    """Real fleet alarms fire on one reading.

    Requiring two consecutive breaches would cut false alarms and cost lead
    time, which would make our own comparison look better than it is. The
    baseline must be as good as a threshold alarm actually is - no better and
    no worse.
    """
    detection = BaselineDetector().evaluate([reading(8.6, minute=137)], envelope=PHARMA, now=START)

    assert detection is not None
    assert detection.detected_by is DetectedBy.BASELINE
    assert detection.detected_at == START + timedelta(minutes=137)
    assert "8.60" in detection.detail


def test_the_baseline_reports_no_predicted_breach() -> None:
    """A threshold alarm has nothing to say about the future.

    `predicted_breach_at` staying None is what makes the lead time of this arm
    zero by construction. Filling it in with anything would be inventing
    foresight the incumbent system does not have.
    """
    detection = BaselineDetector().evaluate([reading(9.0)], envelope=PHARMA, now=START)
    assert detection is not None
    assert detection.predicted_breach_at is None


def test_the_baseline_uses_the_latest_reading_not_an_average() -> None:
    """Smoothing would delay the alarm and flatter the comparison.

    Four in-spec readings and one fresh breach average to something in spec.
    The detector must fire on the breach.
    """
    history = [reading(4.0, minute=minute) for minute in range(4)]
    history.append(reading(9.5, minute=4))

    detection = BaselineDetector().evaluate(history, envelope=PHARMA, now=START)
    assert detection is not None
    assert detection.detected_at == START + timedelta(minutes=4)


def test_the_baseline_ignores_superseded_readings() -> None:
    """A retracted reading must not raise an incident.

    Otherwise correcting a bad reading would trigger the alarm the correction
    was meant to withdraw.
    """
    breach = reading(9.5, minute=3, status=EvidenceStatus.SUPERSEDED)
    detection = BaselineDetector().evaluate(
        [reading(4.0, minute=4), breach], envelope=PHARMA, now=START
    )
    assert detection is None


def test_the_baseline_is_silent_when_it_has_no_temperature_reading() -> None:
    """Absence is absence. No reading is not the same as no breach.

    Returning a detection here would be fabricating an observation; returning
    a cheerful all-clear would be worse, because it reads as positive
    evidence that nothing is wrong.
    """
    assert BaselineDetector().evaluate([], envelope=PHARMA, now=START) is None


@pytest.mark.parametrize(
    ("temperature", "expected"),
    [
        (8.5, IncidentSeverity.SEV3),
        (9.5, IncidentSeverity.SEV2),
        (12.5, IncidentSeverity.SEV1),
        (1.5, IncidentSeverity.SEV3),
        (-2.0, IncidentSeverity.SEV1),
    ],
)
def test_severity_scales_with_distance_outside_the_envelope(
    temperature: float, expected: IncidentSeverity
) -> None:
    """Severity is in degrees over the limit, not a percentage of it.

    A percentage of a limit that can be negative - frozen cargo sits at -18 C
    - produces nonsense, and the frozen cases are the ones where an
    under-triaged excursion is most expensive.
    """
    detection = BaselineDetector().evaluate([reading(temperature)], envelope=PHARMA, now=START)
    assert detection is not None
    assert detection.severity is expected


def test_which_envelope_applies_changes_when_the_alarm_fires() -> None:
    """This is why reconciliation is load-bearing rather than tidy.

    The ERP says this cargo may reach 10 C; the signed Bill of Lading says
    8 C. At 8.6 C the alarm fires against the document and stays silent
    against the ERP. Reconciling to the wrong source does not produce a
    slightly different number - it produces silence.
    """
    erp = TemperatureEnvelope(minimum_c=2.0, maximum_c=10.0)
    observed = [reading(8.6, minute=137)]

    assert BaselineDetector().evaluate(observed, envelope=PHARMA, now=START) is not None
    assert BaselineDetector().evaluate(observed, envelope=erp, now=START) is None


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_the_correlation_key_ignores_everything_that_changes_minute_to_minute() -> None:
    """One degrading truck is one incident, not forty.

    The key must be identical for two detections describing the same
    situation at different temperatures, or deduplication cannot work at all.
    """
    first = BaselineDetector().evaluate([reading(8.6, minute=137)], envelope=PHARMA, now=START)
    later = BaselineDetector().evaluate([reading(11.2, minute=180)], envelope=PHARMA, now=START)

    assert first is not None
    assert later is not None
    assert first.correlation_key == later.correlation_key
    assert first.severity is not later.severity


def test_different_vehicles_never_share_a_correlation_key() -> None:
    assert correlation_key("vehicle", "AX-042", "thermal_excursion") != correlation_key(
        "vehicle", "AX-043", "thermal_excursion"
    )


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def test_every_status_appears_in_the_transition_table() -> None:
    """A status missing from the table would raise KeyError at runtime.

    That failure would arrive mid-incident, on the transition nobody tested.
    """
    assert set(TRANSITIONS) == set(IncidentStatus)
    for allowed in TRANSITIONS.values():
        assert allowed <= set(IncidentStatus)


def test_closed_and_superseded_are_terminal() -> None:
    """Reopening a closed incident would break the audit narrative.

    A new situation gets a new incident, linked to the old one, rather than
    resurrecting a record whose history says it was finished.
    """
    assert TRANSITIONS[IncidentStatus.CLOSED] == frozenset()
    assert TRANSITIONS[IncidentStatus.SUPERSEDED] == frozenset()


def test_a_failed_verification_can_reopen_a_resolved_incident() -> None:
    """Otherwise the resolution rate measures optimism, not outcomes.

    Verification runs on a schedule after the action. If it could not reopen
    what it just closed, an intervention that failed would still be counted
    as a success.
    """
    assert is_legal_transition(IncidentStatus.RESOLVED, IncidentStatus.VERIFYING)
    assert is_legal_transition(IncidentStatus.VERIFYING, IncidentStatus.INVESTIGATING)


def test_an_incident_cannot_jump_straight_from_detection_to_acting() -> None:
    """Acting without investigating or approving is the whole failure mode.

    Invariant I5 is enforced at the approval layer, but the state machine
    must not offer a path that routes around it either.
    """
    assert not is_legal_transition(IncidentStatus.DETECTED, IncidentStatus.ACTING)
    assert not is_legal_transition(IncidentStatus.DETECTED, IncidentStatus.AWAITING_APPROVAL)
    assert not is_legal_transition(IncidentStatus.INVESTIGATING, IncidentStatus.ACTING)


def test_investigation_can_conclude_that_nothing_needs_doing() -> None:
    """Without this path the only way to close an incident is to act on one.

    A system whose sole exit from investigation is intervention acquires a
    bias toward intervening, which is exactly the bias a false-alarm control
    scenario is meant to catch.
    """
    assert is_legal_transition(IncidentStatus.INVESTIGATING, IncidentStatus.RESOLVED)


def test_a_denied_approval_returns_the_incident_to_investigation() -> None:
    """The situation has not gone away because the answer was no."""
    assert is_legal_transition(IncidentStatus.AWAITING_APPROVAL, IncidentStatus.INVESTIGATING)


def test_an_illegal_transition_names_what_was_allowed() -> None:
    """An error that only says "no" sends the reader to the source."""
    error = IllegalTransitionError(IncidentStatus.CLOSED, IncidentStatus.ACTING)
    assert "closed" in str(error)
    assert "acting" in str(error)
    assert "terminal" in str(error)
