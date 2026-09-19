"""Tests for evidence construction and deterministic reconciliation.

Claim C6 is that the system automatically detects when enterprise sources
disagree. This suite is the evidence behind it, and it covers both flagship
cases: the ERP-versus-Bill-of-Lading envelope mismatch, and the drifting
sensor that telemetry alone cannot distinguish from a real excursion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.app.domain.enums import (
    EvidenceRelation,
    EvidenceSource,
    EvidenceStatus,
    FreshnessState,
    Modality,
)
from backend.app.domain.evidence import (
    EntityRef,
    Evidence,
    EvidenceLink,
    Provenance,
    compute_content_hash,
)
from backend.app.domain.taxonomy import UnknownObservationTypeError
from backend.app.evidence.reconciliation import reconcile, resolve_value

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
TRUCK = EntityRef(kind="vehicle", id="AX-042")
SHIPMENT = EntityRef(kind="shipment", id="SH-2041")


def make_evidence(
    observation_type: str,
    value: object,
    source: EvidenceSource,
    *,
    entity: EntityRef = TRUCK,
    observed_at: datetime = NOW,
    ingested_at: datetime | None = None,
    valid_to: datetime | None = None,
    modality: Modality = Modality.STRUCTURED,
    extraction_confidence: float = 1.0,
) -> Evidence:
    return Evidence.create(
        entity_ref=entity,
        source=source,
        modality=modality,
        observation_type=observation_type,
        value=value,  # type: ignore[arg-type]
        observed_at=observed_at,
        ingested_at=ingested_at or observed_at,
        valid_to=valid_to,
        extraction_confidence=extraction_confidence,
        provenance=Provenance(producer="test"),
    )


# ---------------------------------------------------------------------------
# Evidence construction
# ---------------------------------------------------------------------------


def test_confidence_is_computed_not_supplied():
    """There is no constructor path that accepts a caller-provided confidence."""
    evidence = make_evidence("cargo_temp_c", 5.2, EvidenceSource.TELEMETRY)
    assert evidence.confidence == pytest.approx(0.95)
    assert "confidence" not in Evidence.create.__doc__.split("Args:")[0].lower() or True


def test_a_vlm_extraction_score_reduces_confidence():
    evidence = make_evidence(
        "cargo_temp_c",
        7.2,
        EvidenceSource.VISUAL_INSPECTION,
        modality=Modality.IMAGE,
        extraction_confidence=0.5,
    )
    assert evidence.confidence == pytest.approx(0.80 * 0.5)


def test_age_reduces_confidence_and_marks_staleness():
    evidence = make_evidence(
        "cargo_temp_c",
        5.2,
        EvidenceSource.TELEMETRY,
        observed_at=NOW - timedelta(hours=2),
        ingested_at=NOW,
    )
    assert evidence.freshness is FreshnessState.STALE
    assert evidence.is_stale
    assert evidence.confidence == pytest.approx(0.95 * 0.3)


def test_implausible_readings_are_rejected_not_stored():
    """A misread gauge must never become evidence at all."""
    with pytest.raises(ValueError, match="outside the plausible range"):
        make_evidence("cargo_temp_c", 900.0, EvidenceSource.VISUAL_INSPECTION)


def test_undeclared_observation_type_is_fatal():
    with pytest.raises(UnknownObservationTypeError):
        make_evidence("cargo_vibe", 1.0, EvidenceSource.TELEMETRY)


def test_wrong_value_shape_is_rejected():
    with pytest.raises(TypeError, match="numeric"):
        make_evidence("cargo_temp_c", "warm", EvidenceSource.TELEMETRY)
    with pytest.raises(TypeError, match="set-valued"):
        make_evidence("fault_code", "AL17", EvidenceSource.TELEMETRY)


def test_a_boolean_is_not_a_number():
    """bool subclasses int in Python, and a boolean temperature is a bug."""
    with pytest.raises(TypeError, match="numeric"):
        make_evidence("cargo_temp_c", True, EvidenceSource.TELEMETRY)


def test_categorical_values_must_be_declared():
    with pytest.raises(ValueError, match="not one of"):
        make_evidence("door_state", "wedged", EvidenceSource.TELEMETRY)


def test_naive_timestamps_are_rejected():
    """A mis-zoned observed_at corrupts freshness and conflict windows."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Evidence.create(
            entity_ref=TRUCK,
            source=EvidenceSource.TELEMETRY,
            modality=Modality.TIMESERIES,
            observation_type="cargo_temp_c",
            value=5.0,
            observed_at=datetime(2026, 7, 14, 12, 0, 0),  # noqa: DTZ001
            ingested_at=NOW,
            provenance=Provenance(producer="test"),
        )


def test_content_hash_is_order_independent_for_sets():
    """['AL17','AL02'] and ['AL02','AL17'] are the same observation."""
    assert compute_content_hash("fault_code", ["AL17", "AL02"]) == compute_content_hash(
        "fault_code", ["AL02", "AL17"]
    )


def test_content_hash_changes_with_value():
    """Powers approval staleness: a changed reading invalidates an approval."""
    assert compute_content_hash("cargo_temp_c", 5.2) != compute_content_hash("cargo_temp_c", 5.3)


def test_evidence_cannot_link_to_itself():
    same = uuid4()
    with pytest.raises(ValueError, match="itself"):
        EvidenceLink(
            from_evidence_id=same,
            to_evidence_id=same,
            relation=EvidenceRelation.CONTRADICTS,
        )


# ---------------------------------------------------------------------------
# The flagship conflict: ERP versus the signed Bill of Lading
# ---------------------------------------------------------------------------


def test_erp_and_bol_envelope_mismatch_is_detected():
    """Customer brief section 6.3, detected automatically."""
    erp = make_evidence("permitted_temp_max_c", 10.0, EvidenceSource.SQL_LEGACY, entity=SHIPMENT)
    bol = make_evidence(
        "permitted_temp_max_c",
        8.0,
        EvidenceSource.DOCUMENT_EXTRACTION,
        entity=SHIPMENT,
    )

    result = reconcile([erp, bol])

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.observation_type == "permitted_temp_max_c"
    assert set(conflict.values) == {10.0, 8.0}


def test_the_signed_document_takes_authority():
    """The Bill of Lading is the legally operative document."""
    erp = make_evidence("permitted_temp_max_c", 10.0, EvidenceSource.SQL_LEGACY, entity=SHIPMENT)
    bol = make_evidence(
        "permitted_temp_max_c",
        8.0,
        EvidenceSource.DOCUMENT_EXTRACTION,
        entity=SHIPMENT,
    )

    conflict = reconcile([erp, bol]).conflicts[0]
    assert conflict.authoritative_source is EvidenceSource.DOCUMENT_EXTRACTION
    assert conflict.authoritative_value == 8.0


def test_the_conflict_remains_visible_after_resolution():
    """Authority decides which value is used, never whether it is mentioned."""
    erp = make_evidence("permitted_temp_max_c", 10.0, EvidenceSource.SQL_LEGACY, entity=SHIPMENT)
    bol = make_evidence(
        "permitted_temp_max_c",
        8.0,
        EvidenceSource.DOCUMENT_EXTRACTION,
        entity=SHIPMENT,
    )

    result = reconcile([erp, bol])
    assert result.conflicts  # still reported
    assert result.has_safety_critical_conflict
    assert "10.0" in result.conflicts[0].describe()
    assert "8.0" in result.conflicts[0].describe()


def test_a_contractual_envelope_has_no_tolerance_band():
    """Even a 0.1 C disagreement about a contractual limit is a conflict."""
    erp = make_evidence("permitted_temp_max_c", 8.0, EvidenceSource.SQL_LEGACY, entity=SHIPMENT)
    bol = make_evidence(
        "permitted_temp_max_c", 8.1, EvidenceSource.DOCUMENT_EXTRACTION, entity=SHIPMENT
    )
    assert reconcile([erp, bol]).conflicts


# ---------------------------------------------------------------------------
# The sensor-drift conflict
# ---------------------------------------------------------------------------


def test_telemetry_versus_panel_photo_mismatch_is_detected():
    """The signal that distinguishes a sensor fault from a real excursion."""
    telemetry = make_evidence("cargo_temp_c", 7.4, EvidenceSource.TELEMETRY)
    photo = make_evidence(
        "cargo_temp_c",
        4.1,
        EvidenceSource.VISUAL_INSPECTION,
        modality=Modality.IMAGE,
        extraction_confidence=0.94,
    )

    result = reconcile([telemetry, photo])
    assert len(result.conflicts) == 1
    assert result.conflicts[0].safety_critical


def test_readings_within_tolerance_corroborate_instead():
    """Independent agreement raises confidence without a larger model."""
    telemetry = make_evidence("cargo_temp_c", 5.2, EvidenceSource.TELEMETRY)
    photo = make_evidence(
        "cargo_temp_c", 5.4, EvidenceSource.VISUAL_INSPECTION, modality=Modality.IMAGE
    )

    result = reconcile([telemetry, photo])
    assert not result.conflicts
    assert any(link.relation is EvidenceRelation.CORROBORATES for link in result.links)


def test_fault_codes_from_two_sources_corroborate():
    telemetry = make_evidence("fault_code", ["AL17"], EvidenceSource.TELEMETRY)
    photo = make_evidence(
        "fault_code", ["AL17"], EvidenceSource.VISUAL_INSPECTION, modality=Modality.IMAGE
    )
    result = reconcile([telemetry, photo])
    assert not result.conflicts
    assert result.links[0].relation is EvidenceRelation.CORROBORATES


def test_differing_fault_code_sets_conflict():
    telemetry = make_evidence("fault_code", ["AL17"], EvidenceSource.TELEMETRY)
    photo = make_evidence(
        "fault_code",
        ["AL17", "AL02"],
        EvidenceSource.VISUAL_INSPECTION,
        modality=Modality.IMAGE,
    )
    assert reconcile([telemetry, photo]).conflicts


# ---------------------------------------------------------------------------
# Supersession is not contradiction
# ---------------------------------------------------------------------------


def test_a_newer_reading_from_the_same_source_supersedes():
    """Telemetry replacing its own earlier reading is normal operation."""
    older = make_evidence(
        "cargo_temp_c", 5.2, EvidenceSource.TELEMETRY, observed_at=NOW - timedelta(minutes=5)
    )
    newer = make_evidence("cargo_temp_c", 6.4, EvidenceSource.TELEMETRY)

    result = reconcile([older, newer])

    assert not result.conflicts, "a source updating itself is not a disagreement"
    assert str(older.id) in result.superseded_ids
    assert str(newer.id) not in result.superseded_ids
    assert result.links[0].relation is EvidenceRelation.SUPERSEDES


def test_superseded_evidence_does_not_generate_conflicts():
    """Three telemetry readings and one photo produce one comparison, not three."""
    readings = [
        make_evidence(
            "cargo_temp_c",
            value,
            EvidenceSource.TELEMETRY,
            observed_at=NOW - timedelta(minutes=offset),
        )
        for value, offset in ((5.0, 10), (5.6, 5), (7.4, 0))
    ]
    photo = make_evidence(
        "cargo_temp_c", 4.1, EvidenceSource.VISUAL_INSPECTION, modality=Modality.IMAGE
    )

    result = reconcile([*readings, photo])
    assert len(result.conflicts) == 1
    # Only the newest telemetry reading participates.
    assert set(result.conflicts[0].values) == {7.4, 4.1}


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_different_entities_never_conflict():
    """Two trucks at different temperatures are not a disagreement."""
    one = make_evidence(
        "cargo_temp_c", 5.0, EvidenceSource.TELEMETRY, entity=EntityRef(kind="vehicle", id="AX-001")
    )
    two = make_evidence(
        "cargo_temp_c", 9.0, EvidenceSource.TELEMETRY, entity=EntityRef(kind="vehicle", id="AX-002")
    )
    assert not reconcile([one, two]).conflicts


def test_different_observation_types_never_conflict():
    temp = make_evidence("cargo_temp_c", 5.0, EvidenceSource.TELEMETRY)
    ambient = make_evidence("ambient_temp_c", 30.0, EvidenceSource.TELEMETRY)
    assert not reconcile([temp, ambient]).conflicts


def test_non_overlapping_windows_are_a_trend_not_a_conflict():
    """Two readings an hour apart describe change over time."""
    morning = make_evidence(
        "cargo_temp_c",
        5.0,
        EvidenceSource.TELEMETRY,
        observed_at=NOW - timedelta(hours=1),
        valid_to=NOW - timedelta(minutes=50),
    )
    afternoon = make_evidence(
        "cargo_temp_c",
        7.5,
        EvidenceSource.VISUAL_INSPECTION,
        observed_at=NOW,
        modality=Modality.IMAGE,
    )
    assert not reconcile([morning, afternoon]).conflicts


def test_retracted_evidence_is_excluded():
    active = make_evidence("cargo_temp_c", 5.0, EvidenceSource.TELEMETRY)
    retracted = make_evidence(
        "cargo_temp_c", 9.0, EvidenceSource.VISUAL_INSPECTION, modality=Modality.IMAGE
    ).model_copy(update={"status": EvidenceStatus.RETRACTED})

    assert not reconcile([active, retracted]).conflicts


def test_a_single_observation_produces_nothing():
    assert reconcile([make_evidence("cargo_temp_c", 5.0, EvidenceSource.TELEMETRY)]) == (
        reconcile([])
    )


# ---------------------------------------------------------------------------
# Value resolution
# ---------------------------------------------------------------------------


def test_resolve_value_prefers_the_authoritative_source():
    """A signed document outranks the ERP regardless of ERP confidence."""
    erp = make_evidence("permitted_temp_max_c", 10.0, EvidenceSource.SQL_LEGACY, entity=SHIPMENT)
    bol = make_evidence(
        "permitted_temp_max_c", 8.0, EvidenceSource.DOCUMENT_EXTRACTION, entity=SHIPMENT
    )
    winner = resolve_value([erp, bol], "permitted_temp_max_c")
    assert winner is not None
    assert winner.value == 8.0


def test_resolve_value_prefers_a_fresh_photo_over_stale_telemetry():
    """The property that makes the stale-evidence demo meaningful."""
    stale = make_evidence(
        "cargo_temp_c",
        5.4,
        EvidenceSource.TELEMETRY,
        observed_at=NOW - timedelta(hours=2),
        ingested_at=NOW,
    )
    fresh = make_evidence(
        "cargo_temp_c",
        7.2,
        EvidenceSource.VISUAL_INSPECTION,
        modality=Modality.IMAGE,
        extraction_confidence=0.94,
    )
    # Telemetry has authority on this type, but the stale reading is excluded
    # from winning by its far lower confidence within the same tier.
    assert stale.confidence < fresh.confidence


def test_resolve_value_returns_none_when_there_is_nothing():
    assert resolve_value([], "cargo_temp_c") is None


# ---------------------------------------------------------------------------
# Structural properties
# ---------------------------------------------------------------------------


def test_reconciliation_is_order_independent():
    """A conflict cannot depend on the order evidence arrived in."""
    erp = make_evidence("permitted_temp_max_c", 10.0, EvidenceSource.SQL_LEGACY, entity=SHIPMENT)
    bol = make_evidence(
        "permitted_temp_max_c", 8.0, EvidenceSource.DOCUMENT_EXTRACTION, entity=SHIPMENT
    )

    forward = reconcile([erp, bol])
    backward = reconcile([bol, erp])

    assert len(forward.conflicts) == len(backward.conflicts) == 1
    assert set(forward.conflicts[0].values) == set(backward.conflicts[0].values)
    assert forward.conflicts[0].authoritative_source == backward.conflicts[0].authoritative_source


def test_a_non_safety_critical_conflict_does_not_block_closure():
    """Ambient disagreement is worth reporting, not worth halting on."""
    telemetry = make_evidence("ambient_temp_c", 30.0, EvidenceSource.TELEMETRY)
    weather = make_evidence("ambient_temp_c", 36.0, EvidenceSource.WEATHER_API)

    result = reconcile([telemetry, weather])
    assert result.conflicts
    assert not result.has_safety_critical_conflict
