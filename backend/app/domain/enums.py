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


class IncidentStatus(StrEnum):
    """Where an incident is in its lifecycle.

    The vocabulary is fixed here because the database column and the
    deduplication query both need it. The *transitions* between these states -
    which ones are legal, and what each one requires - belong to the incident
    state machine and are deliberately not encoded here.

    ``SUPERSEDED`` is distinct from ``CLOSED``: an incident absorbed into
    another is not a resolved incident, and counting it as one would flatter
    every resolution metric.
    """

    DETECTED = "detected"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    ACTING = "acting"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    CLOSED = "closed"
    SUPERSEDED = "superseded"


#: The statuses that mean an incident is still live. A new detection matching
#: one of these on the same correlation key attaches rather than opening a
#: duplicate, which is what stops one degrading truck producing forty
#: incidents.
ACTIVE_INCIDENT_STATUSES = frozenset(
    {
        IncidentStatus.DETECTED,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.ACTING,
        IncidentStatus.VERIFYING,
    }
)


class IncidentSeverity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class DetectedBy(StrEnum):
    """Which detector raised an incident.

    Stored on every incident because lead time is defined as the gap between
    ``AXON`` and ``BASELINE`` detecting the same event. Without this field that
    comparison is not computable after the fact, and the headline claim would
    rest on a number nobody could reproduce.
    """

    AXON = "axon"
    BASELINE = "baseline"
    HUMAN = "human"


class RootCause(StrEnum):
    """Candidate root causes for a cold-chain incident.

    A closed set shared by three consumers: the hypothesis scorer's rule
    library, IncidentForge's ground truth, and AxonBench's diagnosis graders.
    They must agree on vocabulary or root-cause accuracy cannot be computed.

    The model may propose a hypothesis only from this set; an invented cause
    fails schema validation rather than entering the incident.
    """

    COMPRESSOR_DEGRADATION = "compressor_degradation"
    DOOR_LEFT_OPEN = "door_left_open"
    SENSOR_MALFUNCTION = "sensor_malfunction"
    ENVIRONMENTAL_HEAT = "environmental_heat"
    ROUTE_DELAY = "route_delay"
    REEFER_FUEL_EXHAUSTION = "reefer_fuel_exhaustion"
    INCORRECT_CARGO_CONFIGURATION = "incorrect_cargo_configuration"
    DATA_INCONSISTENCY = "data_inconsistency"
    NO_FAULT = "no_fault"


class ActionType(StrEnum):
    """The closed catalogue of interventions the system may propose.

    Closed by design: the model proposes an ``ActionCandidate`` naming one of
    these, and anything else fails to parse. That is what prevents an injected
    instruction from inventing an action.

    ``DO_NOTHING`` is always scored alongside the others. A system that always
    recommends action is a system with a broken prior, and the expected-value
    comparison is meaningless without the null option in it.
    """

    DO_NOTHING = "do_nothing"
    CONTINUE_ROUTE = "continue_route"
    CONTACT_DRIVER = "contact_driver"
    INSPECT_REFRIGERATION = "inspect_refrigeration"
    REROUTE_TO_COLD_STORAGE = "reroute_to_cold_storage"
    SWITCH_FACILITY = "switch_facility"
    TRAILER_SWAP = "trailer_swap"
    ESCALATE_MAINTENANCE = "escalate_maintenance"
    MARK_FOR_INSPECTION = "mark_for_inspection"
    DRAFT_CUSTOMER_NOTIFICATION = "draft_customer_notification"


class RiskCategory(StrEnum):
    """Coarse risk bands for grading and display.

    Bands are a presentation and grading convenience. The probability itself
    remains the decision input, because the expected-value layer consumes it as
    a probability; collapsing to a band before deciding would discard exactly
    the information calibration exists to provide.
    """

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"
