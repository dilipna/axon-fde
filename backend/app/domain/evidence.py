"""The Evidence model: one typed observation, with its provenance.

Every modality normalises into this shape. Because the type vocabulary is
shared across SQL, telemetry, documents and images, contradiction detection
becomes a deterministic value comparison rather than a judgement handed to a
language model.

Two invariants are enforced here rather than documented:

1. **A language model can never be the source of an observation.** There is no
   LLM member of `EvidenceSource`. The model links, interprets and narrates
   evidence; it never creates it.
2. **Confidence is computed, never supplied.** `Evidence.create()` derives it
   from the taxonomy. There is no constructor path that accepts a
   caller-provided confidence.

See docs/architecture/evidence-model.md and ADR-007.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.domain.enums import (
    ConfidenceBasis,
    EvidenceRelation,
    EvidenceSource,
    EvidenceStatus,
    FreshnessState,
    Modality,
    ObservationKind,
)
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy

__all__ = [
    "EntityRef",
    "Evidence",
    "EvidenceLink",
    "ObservationValue",
    "Provenance",
]

#: The value shapes an observation may carry. Deliberately narrow: anything
#: that is not one of these cannot be compared deterministically.
ObservationValue = float | int | str | bool | list[str]


class EntityRef(BaseModel):
    """What an observation is about.

    A typed `(kind, id)` pair rather than a foreign key, because evidence
    attaches to several kinds of subject and because the referenced record
    usually lives in SQL Server, where we could not declare a foreign key even
    if we wanted one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(pattern=r"^[a-z_]+$")
    id: str = Field(min_length=1)

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


class Provenance(BaseModel):
    """Where an observation came from, in enough detail to go back to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The tool or adapter that produced this, with its version, so a change in
    #: extraction behaviour is traceable.
    producer: str
    producer_version: str = "1.0.0"

    #: Hash of the query, prompt or request. Never the query text itself, which
    #: could carry sensitive values.
    request_hash: str | None = None

    #: Page and bounding box for document extraction, enabling click-through.
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    #: Evidence this was derived from, for observations computed from others.
    derived_from: tuple[UUID, ...] = ()

    note: str | None = None


class Evidence(BaseModel):
    """A single typed observation with provenance and computed trust."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID | None = None
    entity_ref: EntityRef

    source: EvidenceSource
    modality: Modality

    observation_type: str
    value: ObservationValue
    unit: str | None = None

    observed_at: datetime
    valid_from: datetime
    valid_to: datetime | None = None
    ingested_at: datetime

    confidence: float = Field(ge=0.0, le=1.0)
    confidence_basis: ConfidenceBasis
    freshness: FreshnessState

    provenance: Provenance
    artifact_id: UUID | None = None
    content_hash: str

    status: EvidenceStatus = EvidenceStatus.ACTIVE

    @model_validator(mode="after")
    def _check_time_ordering(self) -> Self:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError(f"valid_to ({self.valid_to}) precedes valid_from ({self.valid_from})")
        for field, value in (
            ("observed_at", self.observed_at),
            ("valid_from", self.valid_from),
            ("ingested_at", self.ingested_at),
        ):
            if value.tzinfo is None:
                raise ValueError(
                    f"{field} must be timezone-aware; a naive timestamp would "
                    "corrupt freshness and conflict-window comparison"
                )
        return self

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        entity_ref: EntityRef,
        source: EvidenceSource,
        modality: Modality,
        observation_type: str,
        value: ObservationValue,
        observed_at: datetime,
        ingested_at: datetime,
        provenance: Provenance,
        incident_id: UUID | None = None,
        artifact_id: UUID | None = None,
        valid_to: datetime | None = None,
        extraction_confidence: float = 1.0,
        taxonomy: Taxonomy | None = None,
    ) -> Evidence:
        """Build a validated evidence record.

        This is the only supported construction path, and it is why confidence
        cannot be supplied by a caller: it is derived here from the taxonomy,
        the source's declared reliability, the extraction score, and age.

        Raises:
            UnknownObservationTypeError: The type is not declared. Fatal by
                design - an undeclared type means some adapter is inventing
                vocabulary, which would silently disable conflict detection.
            ValueError: The value is the wrong shape for the declared kind, or
                a numeric reading falls outside its plausible physical range.
        """
        # Checked before any arithmetic: subtracting a naive from an aware
        # datetime raises an opaque TypeError, and the model validator that
        # would catch this runs too late to produce a useful message.
        for field_name, moment in (
            ("observed_at", observed_at),
            ("ingested_at", ingested_at),
        ):
            if moment.tzinfo is None:
                raise ValueError(
                    f"{field_name} must be timezone-aware; a naive timestamp "
                    "would corrupt freshness and conflict-window comparison"
                )

        tax = taxonomy or load_taxonomy()
        spec = tax.spec(observation_type)  # raises on unknown types

        _validate_value_shape(spec.kind, observation_type, value)

        if spec.kind is ObservationKind.NUMERIC:
            numeric = float(value)  # type: ignore[arg-type]
            if not spec.is_plausible(numeric):
                # Rejected outright rather than stored at low confidence: a
                # misread gauge must never become evidence at all.
                raise ValueError(
                    f"{observation_type}={numeric} is outside the plausible "
                    f"range {spec.plausible_range}; rejecting rather than "
                    "recording an implausible observation"
                )

        if (
            spec.kind is ObservationKind.CATEGORICAL
            and spec.allowed_values
            and value not in spec.allowed_values
        ):
            raise ValueError(f"{observation_type}={value!r} is not one of {spec.allowed_values}")

        age_seconds = max(0.0, (ingested_at - observed_at).total_seconds())
        confidence = tax.confidence(
            observation_type,
            source,
            age_seconds=age_seconds,
            extraction_confidence=extraction_confidence,
        )
        freshness = tax.freshness(observation_type, age_seconds)

        basis = (
            ConfidenceBasis.EXTRACTION_MODEL
            if extraction_confidence < 1.0
            else ConfidenceBasis.SOURCE_RELIABILITY
        )

        return cls(
            incident_id=incident_id,
            entity_ref=entity_ref,
            source=source,
            modality=modality,
            observation_type=observation_type,
            value=value,
            unit=spec.unit,
            observed_at=observed_at,
            valid_from=observed_at,
            valid_to=valid_to,
            ingested_at=ingested_at,
            confidence=confidence,
            confidence_basis=basis,
            freshness=freshness,
            provenance=provenance,
            artifact_id=artifact_id,
            content_hash=compute_content_hash(observation_type, value),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.status is EvidenceStatus.ACTIVE

    @property
    def is_stale(self) -> bool:
        return self.freshness is FreshnessState.STALE

    def overlaps(self, other: Evidence) -> bool:
        """Whether two observations describe overlapping periods.

        Two readings an hour apart are not in conflict; they are a trend. Only
        overlapping validity windows can disagree.
        """
        self_end = self.valid_to
        other_end = other.valid_to
        if self_end is not None and self_end <= other.valid_from:
            return False
        return not (other_end is not None and other_end <= self.valid_from)


class EvidenceLink(BaseModel):
    """A typed relation between two pieces of evidence.

    A separate record rather than an array column, because "show every open
    conflict in the fleet" is a first-class query, not an incident-scoped one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    from_evidence_id: UUID
    to_evidence_id: UUID
    relation: EvidenceRelation

    #: Always `rule` today. Reserved for a future where the model may propose
    #: links, which would still require deterministic validation before use.
    detected_by: str = "rule"
    rule_id: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _reject_self_links(self) -> Self:
        if self.from_evidence_id == self.to_evidence_id:
            raise ValueError("evidence cannot be linked to itself")
        return self


def _validate_value_shape(
    kind: ObservationKind, observation_type: str, value: ObservationValue
) -> None:
    """Reject a value whose Python type does not match the declared kind."""
    match kind:
        case ObservationKind.NUMERIC:
            # bool is a subclass of int, and a boolean temperature is a bug.
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(
                    f"{observation_type} is numeric but got {type(value).__name__}: {value!r}"
                )
        case ObservationKind.CATEGORICAL:
            if not isinstance(value, str):
                raise TypeError(
                    f"{observation_type} is categorical but got {type(value).__name__}: {value!r}"
                )
        case ObservationKind.BOOLEAN:
            if not isinstance(value, bool):
                raise TypeError(f"{observation_type} is boolean but got {type(value).__name__}")
        case ObservationKind.SET:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TypeError(f"{observation_type} is set-valued but got {value!r}")
        case ObservationKind.INTERVAL:
            if not isinstance(value, list) or len(value) != 2:
                raise TypeError(f"{observation_type} is an interval but got {value!r}")


def compute_content_hash(observation_type: str, value: ObservationValue) -> str:
    """Stable hash of an observation's content.

    Powers approval-staleness binding: an approval is bound to the hashes of
    the evidence it was granted against, so a changed reading invalidates it.

    Set-valued observations are sorted before hashing, because `["AL17","AL02"]`
    and `["AL02","AL17"]` are the same observation.
    """
    canonical: Any = sorted(value) if isinstance(value, list) else value
    payload = json.dumps(
        {"type": observation_type, "value": canonical},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()
