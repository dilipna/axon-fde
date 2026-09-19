"""Controlled vocabularies shared across the whole system.

These enums are deliberately closed sets. A value that is not listed here
cannot enter the evidence store, which is what allows reconciliation,
grading and policy checks to be deterministic rather than heuristic.
"""

from __future__ import annotations

from enum import StrEnum


class ObservationKind(StrEnum):
    """The value shape of an observation type."""

    NUMERIC = "numeric"
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"
    SET = "set"
    INTERVAL = "interval"


class EvidenceSource(StrEnum):
    """Where an observation came from.

    There is deliberately no ``LLM_INFERENCE`` member. An LLM may link,
    interpret and narrate evidence, but it may never author an observation.
    That invariant is enforced here at the type level and again in the
    evidence service, and it is covered by a test.
    """

    SQL_LEGACY = "sql_legacy"
    TELEMETRY = "telemetry"
    DOCUMENT_EXTRACTION = "document_extraction"
    VISUAL_INSPECTION = "visual_inspection"
    WEATHER_API = "weather_api"
    RISK_MODEL = "risk_model"
    SOP_RETRIEVAL = "sop_retrieval"
    HUMAN_INPUT = "human_input"


class Modality(StrEnum):
    STRUCTURED = "structured"
    TIMESERIES = "timeseries"
    TEXT = "text"
    IMAGE = "image"


class FreshnessState(StrEnum):
    """How far past its useful life an observation is.

    ``NOT_APPLICABLE`` covers types with no TTL, such as a contractual
    temperature range, which does not become less true with age.
    """

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    NOT_APPLICABLE = "not_applicable"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"
    RETRACTED = "retracted"


class EvidenceRelation(StrEnum):
    """How two pieces of evidence relate.

    ``SUPERSEDES`` is distinct from ``CONTRADICTS`` on purpose: a newer reading
    from the *same* source replaces an older one, which is not a disagreement
    between sources and must not be reported to the dispatcher as a conflict.
    """

    CORROBORATES = "corroborates"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"


class ConfidenceBasis(StrEnum):
    """What a confidence number was derived from.

    Recorded so that any confidence in the system can be explained. No member
    of this enum permits a model-authored score.
    """

    SENSOR_SPEC = "sensor_spec"
    EXTRACTION_MODEL = "extraction_model"
    SOURCE_RELIABILITY = "source_reliability"
    DERIVED = "derived"
