"""SQLAlchemy models for the application database.

This is the half of the system we own. The legacy ERP is read-only and
unmigrated; everything the incident loop produces lives here.

Two tables carry constraints that are load-bearing rather than cosmetic:

``audit_event``      Append-only and hash-chained. The application role is
                     granted INSERT and SELECT only, a trigger raises on
                     UPDATE or DELETE, and each row hashes the previous one.
                     Three independent layers, because any one of them can be
                     misconfigured.

``action_execution`` A unique idempotency key, so a retried execution returns
                     the first result rather than causing a second side effect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base.

    `JSONB` is used rather than `JSON` throughout: it is indexable, and every
    deployment target is PostgreSQL.
    """

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[str]: JSONB,
    }


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class Incident(Base, TimestampMixin):
    __tablename__ = "incident"

    id: Mapped[UUID] = _uuid_pk()

    #: Deduplication key. One degrading truck must produce one incident, not
    #: forty, so a new detection matching an active incident inside the
    #: suppression window attaches to it instead of creating another.
    correlation_key: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(64), nullable=False)

    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Which detector raised this: `axon` (predictive) or `baseline`
    #: (threshold). Storing it is what makes lead time computable.
    detected_by: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    predicted_breach_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    superseded_by: Mapped[UUID | None] = mapped_column(ForeignKey("incident.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Ties this incident to IncidentForge ground truth. Without it, evaluation
    #: is guesswork.
    scenario_run_id: Mapped[str | None] = mapped_column(String(128))

    evidence: Mapped[list[Evidence]] = relationship(back_populates="incident")

    __table_args__ = (
        Index("ix_incident_status", "status"),
        Index("ix_incident_entity", "entity_kind", "entity_id"),
        # Supports the deduplication lookup on the hot path.
        Index("ix_incident_correlation_active", "correlation_key", "status"),
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class Artifact(Base, TimestampMixin):
    """Immutable bytes plus metadata: a photograph, a PDF, a model file."""

    __tablename__ = "artifact"

    id: Mapped[UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Makes the artifact addressable and tampering detectable.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[str | None] = mapped_column(String(128))


class Evidence(Base):
    """One typed observation. See docs/architecture/evidence-model.md."""

    __tablename__ = "evidence"

    id: Mapped[UUID] = _uuid_pk()
    incident_id: Mapped[UUID | None] = mapped_column(ForeignKey("incident.id"))

    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    modality: Mapped[str] = mapped_column(String(16), nullable=False)

    observation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: JSONB because an observation may be numeric, categorical, boolean or a
    #: set, and the taxonomy decides which. A typed column per shape would mean
    #: a sparse table and a much more awkward comparison path.
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    freshness: Mapped[str] = mapped_column(String(16), nullable=False)

    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_id: Mapped[UUID | None] = mapped_column(ForeignKey("artifact.id"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    incident: Mapped[Incident | None] = relationship(back_populates="evidence")

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_evidence_confidence"),
        # A language model can never be the source of an observation. Enforced
        # in the enum, in the service, and here at the database, because this
        # is the invariant the whole grounding story rests on.
        CheckConstraint(
            "source IN ('sql_legacy','telemetry','document_extraction',"
            "'visual_inspection','weather_api','risk_model','sop_retrieval',"
            "'human_input')",
            name="ck_evidence_source_never_llm",
        ),
        # The conflict-detection lookup: same type, same entity, still active.
        Index("ix_evidence_reconcile", "observation_type", "entity_kind", "entity_id", "status"),
        Index("ix_evidence_incident", "incident_id"),
    )


class EvidenceLink(Base, TimestampMixin):
    """A typed relation between two observations.

    A table rather than array columns, because "show every open conflict in
    the fleet" is a first-class query, not an incident-scoped one.
    """

    __tablename__ = "evidence_link"

    id: Mapped[UUID] = _uuid_pk()
    from_evidence_id: Mapped[UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    to_evidence_id: Mapped[UUID] = mapped_column(ForeignKey("evidence.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_by: Mapped[str] = mapped_column(String(16), nullable=False, default="rule")
    rule_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("from_evidence_id <> to_evidence_id", name="ck_link_not_self"),
        UniqueConstraint("from_evidence_id", "to_evidence_id", "relation", name="uq_evidence_link"),
        Index("ix_link_relation", "relation"),
    )


# ---------------------------------------------------------------------------
# Risk, decision, approval, execution
# ---------------------------------------------------------------------------


class RiskAssessment(Base, TimestampMixin):
    __tablename__ = "risk_assessment"

    id: Mapped[UUID] = _uuid_pk()
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incident.id"), nullable=False)

    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False)

    #: Stored beside every prediction, permanently. Honest comparison should be
    #: structural, not something that lives only in a training notebook and
    #: quietly disappears from the conversation about performance.
    baseline_probability: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_name: Mapped[str] = mapped_column(String(64), nullable=False)

    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    calibration_version: Mapped[str | None] = mapped_column(String(64))
    feature_vector_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: True when the model artifact was unavailable and a rule baseline was
    #: used instead. The system says so rather than pretending.
    degraded: Mapped[bool] = mapped_column(nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("probability >= 0 AND probability <= 1", name="ck_risk_probability"),
        CheckConstraint(
            "baseline_probability >= 0 AND baseline_probability <= 1",
            name="ck_risk_baseline",
        ),
        Index("ix_risk_incident", "incident_id"),
    )


class ActionCandidate(Base, TimestampMixin):
    __tablename__ = "action_candidate"

    id: Mapped[UUID] = _uuid_pk()
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incident.id"), nullable=False)

    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(128))

    feasible: Mapped[bool] = mapped_column(nullable=False, default=True)
    infeasibility_reason: Mapped[str | None] = mapped_column(Text)

    est_cost_usd: Mapped[float | None] = mapped_column(Float)
    est_delay_min: Mapped[float | None] = mapped_column(Float)
    predicted_risk_after: Mapped[float | None] = mapped_column(Float)
    #: p10/p90 band. Options whose bands overlap are reported as not
    #: distinguishable rather than ranked against each other.
    predicted_risk_ci: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    ev_total: Mapped[float | None] = mapped_column(Float)
    ev_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    required_approval_role: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_candidate_incident", "incident_id"),)


class Recommendation(Base, TimestampMixin):
    """The chosen option and its explanation.

    Separate from `ActionCandidate` because one incident yields many candidates
    and one recommendation. Collapsing them would lose the record of what was
    considered and rejected, which is exactly what an auditor asks about.
    """

    __tablename__ = "recommendation"

    id: Mapped[UUID] = _uuid_pk()
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incident.id"), nullable=False)
    selected_action_id: Mapped[UUID] = mapped_column(
        ForeignKey("action_candidate.id"), nullable=False
    )

    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    cited_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    flip_sensitivity: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Result of the deterministic grounding check: citations present in the
    #: bundle, numbers matching evidence values.
    grounding_check: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_recommendation_incident", "incident_id"),)


class Approval(Base, TimestampMixin):
    """A human decision, bound to the world state it was granted against.

    An approval granted at 14:02 against a 78% excursion probability is not an
    approval of the same action at 14:20 after three new readings. The bound
    hash covers the evidence, the risk assessment and the selected action; if
    any of that moved, the approval is stale and must be re-sought.
    """

    __tablename__ = "approval"

    id: Mapped[UUID] = _uuid_pk()
    recommendation_id: Mapped[UUID] = mapped_column(ForeignKey("recommendation.id"), nullable=False)
    action_candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("action_candidate.id"), nullable=False
    )

    bound_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_role: Mapped[str] = mapped_column(String(32), nullable=False)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    decision: Mapped[str | None] = mapped_column(String(16))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("expires_at > requested_at", name="ck_approval_expiry"),
        Index("ix_approval_pending", "decision", "expires_at"),
    )


class ActionExecution(Base, TimestampMixin):
    __tablename__ = "action_execution"

    id: Mapped[UUID] = _uuid_pk()
    approval_id: Mapped[UUID | None] = mapped_column(ForeignKey("approval.id"))
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incident.id"), nullable=False)

    #: Unique. A retried execution returns the first result rather than causing
    #: a second side effect.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_execution_incident", "incident_id"),)


class OutcomeVerification(Base, TimestampMixin):
    """Did the intervention work? The step that closes the loop."""

    __tablename__ = "outcome_verification"

    id: Mapped[UUID] = _uuid_pk()
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incident.id"), nullable=False)
    execution_id: Mapped[UUID | None] = mapped_column(ForeignKey("action_execution.id"))

    expected_effect: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_effect: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    verdict: Mapped[str | None] = mapped_column(String(16))
    follow_up_required: Mapped[bool] = mapped_column(nullable=False, default=False)

    __table_args__ = (Index("ix_verification_incident", "incident_id"),)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditEvent(Base):
    """Append-only, hash-chained record of everything consequential.

    This table is the compliance product (customer brief, opportunity O2), and
    it is protected by three independent layers:

    1. The application role holds INSERT and SELECT only.
    2. A trigger raises on UPDATE or DELETE.
    3. Each row hashes the previous one, so a tampered history is *detectable*
       even by an attacker with direct database access.

    `seq` is a monotonic integer rather than a timestamp because chain order
    must be total and unambiguous; two events in the same millisecond would
    otherwise have no defined predecessor.
    """

    __tablename__ = "audit_event"

    id: Mapped[UUID] = _uuid_pk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), nullable=False)

    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID | None] = mapped_column(ForeignKey("incident.id"))

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_audit_incident", "incident_id"),
        Index("ix_audit_seq", "seq"),
        Index("ix_audit_subject", "subject_kind", "subject_id"),
    )


class ModelInvocation(Base, TimestampMixin):
    """Per-call cost and provenance.

    Makes cost per incident a query rather than a spreadsheet, and ties every
    recommendation to the exact prompt version and model that produced it.
    """

    __tablename__ = "model_invocation"

    id: Mapped[UUID] = _uuid_pk()
    incident_id: Mapped[UUID | None] = mapped_column(ForeignKey("incident.id"))
    node: Mapped[str] = mapped_column(String(64), nullable=False)

    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Zero across repeated incidents means a silent cache invalidator crept
    #: into the prompt prefix, which is where the cost budget actually leaks.
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cost_estimate_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_invocation_incident", "incident_id"),)
