"""Tests for the canonical observation taxonomy.

The taxonomy underpins conflict detection and confidence scoring, so these
tests are deliberately strict: a regression here silently weakens every
downstream guarantee rather than failing loudly.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from backend.app.domain.enums import EvidenceSource, FreshnessState, ObservationKind
from backend.app.domain.taxonomy import (
    DEFAULT_SOURCE_RELIABILITY,
    ObservationSpec,
    UnknownObservationTypeError,
    load_taxonomy,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


# ---------------------------------------------------------------------------
# Structural integrity of the shipped taxonomy
# ---------------------------------------------------------------------------


def test_shipped_taxonomy_loads_and_validates(taxonomy):
    assert taxonomy.version
    assert len(taxonomy.observations) >= 15
    for name, spec in taxonomy.observations.items():
        assert spec.name == name


def test_every_numeric_type_declares_a_plausible_range(taxonomy):
    """Without a range, an absurd reading becomes evidence."""
    for spec in taxonomy.observations.values():
        if spec.kind is ObservationKind.NUMERIC:
            assert spec.plausible_range is not None, spec.name


def test_authority_order_only_names_known_sources(taxonomy):
    for spec in taxonomy.observations.values():
        for source in spec.authority_order:
            assert source in spec.source_reliability, f"{spec.name}: {source}"


def test_safety_critical_types_are_the_expected_ones(taxonomy):
    """Safety-critical types block automatic incident closure when in conflict.

    Pinned explicitly so that widening the set is a deliberate, reviewed act.
    """
    critical = {n for n, s in taxonomy.observations.items() if s.safety_critical}
    assert critical == {
        "cargo_temp_c",
        "permitted_temp_min_c",
        "permitted_temp_max_c",
        "cargo_class",
        "fault_code",
        "excursion_risk_probability",
    }


# ---------------------------------------------------------------------------
# The core architectural invariant: an LLM may never author an observation
# ---------------------------------------------------------------------------


def test_llm_is_not_a_valid_evidence_source():
    """M5: the LLM may link, interpret and narrate evidence, never create it.

    If someone adds an LLM source to the enum, this test fails and forces the
    conversation, rather than letting model output quietly become evidence.
    """
    members = {s.value for s in EvidenceSource}
    for forbidden in ("llm", "llm_inference", "model_inference", "agent", "assistant"):
        assert forbidden not in members


def test_no_observation_type_trusts_an_undeclared_source(taxonomy):
    """Unlisted sources fall back to a deliberately low default."""
    assert taxonomy.reliability("cargo_temp_c", EvidenceSource.SOP_RETRIEVAL) == (
        DEFAULT_SOURCE_RELIABILITY
    )


# ---------------------------------------------------------------------------
# The flagship case: ERP vs signed Bill of Lading
# ---------------------------------------------------------------------------


def test_permitted_range_has_zero_tolerance(taxonomy):
    """A contractual range has no tolerance band: any difference is a conflict."""
    for name in ("permitted_temp_min_c", "permitted_temp_max_c"):
        assert taxonomy.spec(name).conflict_tolerance == 0.0


def test_erp_vs_bol_mismatch_is_detected(taxonomy):
    """ERP says 10.0 C, the signed BOL says 8.0 C. That is a conflict."""
    assert taxonomy.conflicts("permitted_temp_max_c", 10.0, 8.0)


def test_signed_document_outranks_the_erp(taxonomy):
    """The Bill of Lading is the legally operative document."""
    winner = taxonomy.authoritative_source(
        "permitted_temp_max_c",
        [EvidenceSource.SQL_LEGACY, EvidenceSource.DOCUMENT_EXTRACTION],
    )
    assert winner is EvidenceSource.DOCUMENT_EXTRACTION


def test_contractual_values_do_not_age(taxonomy):
    """A permitted range does not become less true with time."""
    spec = taxonomy.spec("permitted_temp_max_c")
    assert spec.ttl_seconds is None
    assert not spec.ages
    assert taxonomy.freshness("permitted_temp_max_c", 10_000_000) is (FreshnessState.NOT_APPLICABLE)


# ---------------------------------------------------------------------------
# Conflict semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (5.0, 5.4, False),  # inside the 0.5 C tolerance
        (5.0, 5.5, False),  # exactly at tolerance is agreement, not conflict
        (5.0, 5.6, True),  # beyond tolerance
        (5.4, 7.2, True),  # the sensor-drift signature
    ],
)
def test_numeric_conflict_respects_tolerance(taxonomy, left, right, expected):
    assert taxonomy.conflicts("cargo_temp_c", left, right) is expected


def test_set_conflict_ignores_ordering(taxonomy):
    """Fault codes are an unordered set; ordering is not disagreement."""
    assert not taxonomy.conflicts("fault_code", ["AL17", "AL02"], ["AL02", "AL17"])
    assert taxonomy.conflicts("fault_code", ["AL17"], ["AL17", "AL02"])


def test_categorical_conflict_is_exact(taxonomy):
    assert taxonomy.conflicts("door_state", "open", "closed")
    assert not taxonomy.conflicts("door_state", "open", "open")


def test_conflict_rejects_wrong_value_shape(taxonomy):
    with pytest.raises(TypeError, match="numeric"):
        taxonomy.conflicts("cargo_temp_c", "5.0", 5.0)
    with pytest.raises(TypeError, match="set-valued"):
        taxonomy.conflicts("fault_code", "AL17", ["AL17"])


def test_unknown_observation_type_is_fatal(taxonomy):
    """An undeclared type means an adapter is inventing vocabulary."""
    with pytest.raises(UnknownObservationTypeError, match="cargo_vibe"):
        taxonomy.conflicts("cargo_vibe", 1.0, 2.0)


# ---------------------------------------------------------------------------
# Freshness and confidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0, FreshnessState.FRESH),
        (300, FreshnessState.FRESH),  # exactly at TTL
        (301, FreshnessState.AGING),
        (900, FreshnessState.AGING),  # exactly at 3x TTL
        (901, FreshnessState.STALE),
    ],
)
def test_freshness_boundaries(taxonomy, age, expected):
    """cargo_temp_c has a 300s TTL, aging at 1x and stale beyond 3x."""
    assert taxonomy.freshness("cargo_temp_c", age) is expected


def test_future_observation_is_a_clock_skew_error(taxonomy):
    with pytest.raises(ValueError, match="negative age"):
        taxonomy.freshness("cargo_temp_c", -1)


def test_confidence_combines_reliability_extraction_and_freshness(taxonomy):
    # telemetry reliability for cargo_temp_c is 0.95; fresh multiplier is 1.0
    assert math.isclose(
        taxonomy.confidence("cargo_temp_c", EvidenceSource.TELEMETRY, age_seconds=0),
        0.95,
    )
    # stale (>3x TTL) applies the 0.3 penalty
    assert math.isclose(
        taxonomy.confidence("cargo_temp_c", EvidenceSource.TELEMETRY, age_seconds=5000),
        0.95 * 0.3,
    )
    # a low-confidence VLM extraction compounds with source reliability
    assert math.isclose(
        taxonomy.confidence(
            "cargo_temp_c",
            EvidenceSource.VISUAL_INSPECTION,
            age_seconds=0,
            extraction_confidence=0.5,
        ),
        0.80 * 0.5,
    )


def test_stale_telemetry_is_less_trusted_than_a_fresh_photo(taxonomy):
    """The property that makes the stale-evidence demo meaningful."""
    stale_sensor = taxonomy.confidence("cargo_temp_c", EvidenceSource.TELEMETRY, age_seconds=5000)
    fresh_photo = taxonomy.confidence(
        "cargo_temp_c", EvidenceSource.VISUAL_INSPECTION, age_seconds=0
    )
    assert fresh_photo > stale_sensor


def test_extraction_confidence_must_be_a_probability(taxonomy):
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        taxonomy.confidence(
            "cargo_temp_c",
            EvidenceSource.VISUAL_INSPECTION,
            age_seconds=0,
            extraction_confidence=1.4,
        )


# ---------------------------------------------------------------------------
# Plausibility gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "plausible"),
    [(5.2, True), (-40.0, True), (60.0, True), (-41.0, False), (900.0, False)],
)
def test_plausible_range_rejects_absurd_readings(taxonomy, value, plausible):
    assert taxonomy.spec("cargo_temp_c").is_plausible(value) is plausible


# ---------------------------------------------------------------------------
# Schema validation rejects malformed declarations
# ---------------------------------------------------------------------------


def test_numeric_type_without_plausible_range_is_rejected():
    with pytest.raises(ValidationError, match="plausible_range"):
        ObservationSpec.model_validate(
            {"name": "bad", "kind": "numeric", "conflict_tolerance": 1.0}
        )


def test_non_numeric_type_with_numeric_tolerance_is_rejected():
    with pytest.raises(ValidationError, match="exact"):
        ObservationSpec.model_validate(
            {
                "name": "bad",
                "kind": "categorical",
                "conflict_tolerance": 1.0,
                "allowed_values": ["a"],
            }
        )


def test_categorical_without_allowed_values_is_rejected():
    with pytest.raises(ValidationError, match="allowed_values"):
        ObservationSpec.model_validate(
            {"name": "bad", "kind": "categorical", "conflict_tolerance": "exact"}
        )


def test_authority_order_naming_an_untrusted_source_is_rejected():
    with pytest.raises(ValidationError, match="authority_order"):
        ObservationSpec.model_validate(
            {
                "name": "bad",
                "kind": "numeric",
                "conflict_tolerance": 0.5,
                "plausible_range": [0.0, 1.0],
                "source_reliability": {"telemetry": 0.9},
                "authority_order": ["weather_api"],
            }
        )


def test_malformed_taxonomy_file_fails_at_load(tmp_path: Path):
    """Validation is eager: a broken taxonomy fails at startup, not mid-incident."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        yaml.safe_dump(
            {
                "version": "0.0.1",
                "freshness_penalty": {
                    "fresh": 1.0,
                    "aging": 0.6,
                    "stale": 0.3,
                    "aging_threshold_multiplier": 1.0,
                    "stale_threshold_multiplier": 3.0,
                },
                "stale_evidence_confidence_cap": 0.45,
                "observations": {"no_range": {"kind": "numeric", "conflict_tolerance": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="plausible_range"):
        load_taxonomy(broken)


def test_missing_taxonomy_file_is_reported_clearly(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="not found"):
        load_taxonomy(tmp_path / "does-not-exist.yaml")


# ---------------------------------------------------------------------------
# Properties that must hold for every declared type
# ---------------------------------------------------------------------------


@given(
    left=st.floats(-40, 60, allow_nan=False),
    right=st.floats(-40, 60, allow_nan=False),
)
def test_numeric_conflict_is_symmetric(left: float, right: float):
    """Disagreement cannot depend on argument order."""
    tax = load_taxonomy()
    assert tax.conflicts("cargo_temp_c", left, right) == tax.conflicts("cargo_temp_c", right, left)


@given(value=st.floats(-40, 60, allow_nan=False))
def test_a_value_never_conflicts_with_itself(value: float):
    assert not load_taxonomy().conflicts("cargo_temp_c", value, value)


@given(
    age=st.floats(0, 100_000, allow_nan=False),
    extraction=st.floats(0.0, 1.0, allow_nan=False),
)
def test_confidence_is_always_a_probability(age: float, extraction: float):
    tax = load_taxonomy()
    for name, spec in tax.observations.items():
        for source in spec.source_reliability:
            value = tax.confidence(name, source, age, extraction)
            assert 0.0 <= value <= 1.0, f"{name}/{source}={value}"
