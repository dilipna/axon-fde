"""Document extractions become evidence that can contradict a database row.

The fixtures under `data/documents/extractions/` are the Phase 1 stand-in for
a real extraction pass. What these tests protect is the shape and the
provenance, because Phase 4 swaps the extractor and must not have to change
anything downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.domain.enums import ConfidenceBasis, EvidenceSource, Modality
from backend.app.evidence.documents import (
    extraction_to_evidence,
    load_extraction,
    load_extractions,
)

pytestmark = pytest.mark.unit

EXTRACTIONS = Path(__file__).resolve().parents[2] / "data" / "documents" / "extractions"
FLAGSHIP_BOL = EXTRACTIONS / "SH-2041_bill_of_lading.json"


def test_the_flagship_bill_of_lading_holds_the_stricter_envelope() -> None:
    """The conflict this whole scenario turns on, asserted at its source.

    The ERP seed holds 2-10 C for SH-2041; this signed document holds 2-8 C.
    If the fixture ever drifted to agree with the ERP, every test that claims
    a conflict is detected would still pass while detecting nothing.
    """
    extraction = load_extraction(FLAGSHIP_BOL)
    values = {field.observation_type: field.value for field in extraction.fields}

    assert values["permitted_temp_max_c"] == 8.0
    assert values["permitted_temp_min_c"] == 2.0
    assert extraction.shipment_id == "SH-2041"
    assert extraction.document_type == "bill_of_lading"


def test_a_control_document_that_agrees_with_the_erp_exists() -> None:
    """Without one, "a conflict was found" is not a discriminating result.

    An adapter that reported a conflict for every shipment would pass a test
    that only ever looks at the shipment known to conflict.
    """
    control = load_extractions(EXTRACTIONS, shipment_id="SH-2052")
    assert control, "the SH-2052 control extraction is missing"
    values = {field.observation_type: field.value for field in control[0].fields}
    assert values["permitted_temp_max_c"] == 8.0


def test_every_extracted_field_carries_a_page_and_a_box() -> None:
    """Provenance is what makes a cited number checkable.

    A recommendation that says "the permitted maximum is 8 C" is an
    assertion. One that can point at the line on the page is a citation, and
    that difference is the entire click-through story.
    """
    for extraction in load_extractions(EXTRACTIONS):
        for field in extraction.fields:
            assert field.page is not None, f"{extraction.document_id}:{field.observation_type}"
            assert field.bbox is not None
            assert len(field.bbox) == 4


def test_extraction_produces_document_sourced_evidence() -> None:
    evidence = extraction_to_evidence(load_extraction(FLAGSHIP_BOL))

    assert len(evidence) == 3
    assert all(item.source is EvidenceSource.DOCUMENT_EXTRACTION for item in evidence)
    assert all(item.modality is Modality.TEXT for item in evidence)
    assert all(item.entity_ref.kind == "shipment" for item in evidence)
    assert all(item.entity_ref.id == "SH-2041" for item in evidence)


def test_the_extraction_score_is_an_input_to_confidence_not_the_confidence() -> None:
    """Invariant I2. Nothing outside the taxonomy may set a confidence.

    The fixture declares 0.96 for the permitted maximum. If that number came
    back as the confidence, the taxonomy - and with it source reliability and
    age - would have been bypassed entirely.
    """
    extraction = load_extraction(FLAGSHIP_BOL)
    declared = {field.observation_type: field.extraction_confidence for field in extraction.fields}
    evidence = extraction_to_evidence(extraction)

    for item in evidence:
        assert item.confidence != declared[item.observation_type]
        assert item.confidence_basis is ConfidenceBasis.EXTRACTION_MODEL


def test_evidence_is_dated_to_when_the_document_was_signed() -> None:
    """A bill of lading states what was agreed when it was signed.

    Dating it to the extraction run would let a freshly re-parsed old document
    outrank a current sensor reading, purely because someone re-ran the
    parser.
    """
    extraction = load_extraction(FLAGSHIP_BOL)
    for item in extraction_to_evidence(extraction):
        assert item.observed_at == datetime(2026, 7, 14, 9, 42, tzinfo=UTC)
        assert item.observed_at < item.ingested_at


def test_provenance_records_the_document_the_extractor_and_the_literal_text() -> None:
    """A disputed number must trace to what was read, not to a conclusion."""
    evidence = extraction_to_evidence(load_extraction(FLAGSHIP_BOL))
    maximum = next(item for item in evidence if item.observation_type == "permitted_temp_max_c")

    assert maximum.provenance.page == 1
    assert maximum.provenance.bbox is not None
    assert maximum.provenance.note is not None
    assert "SD-9001" in maximum.provenance.note
    assert "Max temperature: 8.0 C" in maximum.provenance.note


def test_a_contractual_limit_never_goes_stale() -> None:
    """The taxonomy gives these types no TTL, and that must survive the adapter.

    An envelope that aged out would make a long shipment gradually lose the
    limit it is being judged against.
    """
    for item in extraction_to_evidence(load_extraction(FLAGSHIP_BOL)):
        assert not item.is_stale


def test_a_naive_extraction_timestamp_is_refused(tmp_path: Path) -> None:
    """A naive timestamp would corrupt comparison against telemetry."""
    broken = tmp_path / "broken.json"
    broken.write_text(
        FLAGSHIP_BOL.read_text(encoding="utf-8").replace(
            '"extracted_at": "2026-07-14T09:45:00Z"', '"extracted_at": "2026-07-14T09:45:00"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no timezone"):
        load_extraction(broken)
