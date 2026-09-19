"""Turning extracted document fields into typed evidence.

A document extraction arrives as a set of located values: what was read, on
which page, inside which box, and how sure the extractor was. This adapter
converts that into the same ``Evidence`` shape telemetry and the ERP produce,
which is what lets a signed shipping document contradict a database row
without anyone writing a rule about documents specifically.

**The extractor is not the source of truth about confidence.** A field's
``extraction_confidence`` is one input to ``Taxonomy.confidence()``, alongside
the source's declared reliability and the observation's age. It is never used
as the confidence itself - see invariant I2.

**Page and box travel with the value.** They are the difference between a
recommendation that asserts a number and one that can be clicked through to
the line on the page it came from. An extraction that loses them is still
evidence, but it is evidence nobody can check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.evidence import EntityRef, Evidence, ObservationValue, Provenance
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy

__all__ = [
    "DocumentExtraction",
    "ExtractedField",
    "extraction_to_evidence",
    "load_extraction",
    "load_extractions",
]

PRODUCER = "document_extraction_adapter"
PRODUCER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class ExtractedField:
    """One value read off a document, with where it was read from."""

    observation_type: str
    value: ObservationValue
    extraction_confidence: float
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    verbatim: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentExtraction:
    """Everything one extraction pass produced from one document."""

    document_id: str
    shipment_id: str
    document_type: str
    storage_uri: str
    sha256: str
    extractor: str
    extractor_version: str
    extracted_at: datetime
    fields: tuple[ExtractedField, ...]
    signed_at: datetime | None = None


def _parse_moment(raw: str) -> datetime:
    moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(
            f"extraction timestamp {raw!r} has no timezone; a naive timestamp "
            "would corrupt freshness comparison against telemetry"
        )
    return moment.astimezone(UTC)


def load_extraction(path: Path) -> DocumentExtraction:
    """Read one extraction fixture.

    Raises:
        ValueError: A required key is missing, or a timestamp is naive.
            Fatal rather than defaulted: a silently skipped field is a
            document whose contradiction never surfaces, which is precisely
            the failure this pipeline exists to prevent.
    """
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    fields: list[ExtractedField] = []
    for entry in raw["fields"]:
        bbox = entry.get("bbox")
        fields.append(
            ExtractedField(
                observation_type=entry["observation_type"],
                value=entry["value"],
                extraction_confidence=float(entry["extraction_confidence"]),
                page=entry.get("page"),
                bbox=tuple(bbox) if bbox else None,
                verbatim=entry.get("verbatim"),
            )
        )

    signed = raw.get("signed_at")
    return DocumentExtraction(
        document_id=raw["document_id"],
        shipment_id=raw["shipment_id"],
        document_type=raw["document_type"],
        storage_uri=raw["storage_uri"],
        sha256=raw["sha256"],
        extractor=raw["extractor"],
        extractor_version=raw["extractor_version"],
        extracted_at=_parse_moment(raw["extracted_at"]),
        signed_at=_parse_moment(signed) if signed else None,
        fields=tuple(fields),
    )


def load_extractions(
    directory: Path, *, shipment_id: str | None = None
) -> list[DocumentExtraction]:
    """Read every extraction in a directory, optionally for one shipment."""
    extractions = [load_extraction(path) for path in sorted(directory.glob("*.json"))]
    if shipment_id is not None:
        extractions = [item for item in extractions if item.shipment_id == shipment_id]
    return extractions


def extraction_to_evidence(
    extraction: DocumentExtraction,
    *,
    ingested_at: datetime | None = None,
    taxonomy: Taxonomy | None = None,
) -> list[Evidence]:
    """Convert an extraction into one piece of evidence per located field.

    Observed at the moment the document was *signed*, not the moment it was
    extracted. A bill of lading signed on Tuesday states what was agreed on
    Tuesday; dating it to whenever the extractor happened to run would make a
    freshly re-parsed old document outrank a current sensor reading.
    """
    tax = taxonomy or load_taxonomy()
    entity = EntityRef(kind="shipment", id=extraction.shipment_id)
    observed_at = extraction.signed_at or extraction.extracted_at
    arrival = ingested_at or extraction.extracted_at

    evidence: list[Evidence] = []
    for field in extraction.fields:
        provenance = Provenance(
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            page=field.page,
            bbox=field.bbox,
            # The document, the extractor and the literal text, so that a
            # disputed number can be traced back to what was actually read
            # rather than to what the pipeline concluded.
            note=(
                f"{extraction.document_type} {extraction.document_id} "
                f"via {extraction.extractor} {extraction.extractor_version}"
                + (f": {field.verbatim!r}" if field.verbatim else "")
            ),
        )
        evidence.append(
            Evidence.create(
                entity_ref=entity,
                source=EvidenceSource.DOCUMENT_EXTRACTION,
                modality=Modality.TEXT,
                observation_type=field.observation_type,
                value=field.value,
                observed_at=observed_at,
                ingested_at=arrival,
                provenance=provenance,
                extraction_confidence=field.extraction_confidence,
                taxonomy=tax,
            )
        )
    return evidence
