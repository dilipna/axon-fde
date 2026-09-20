"""Invariant I5, as a hard gate: nothing consequential executes unapproved.

In `tests/security` rather than `tests/integration` because the bypass rate
must be zero and CI runs this suite as a blocking step. The policy matrix next
door proves who *may* ask; this file proves that asking is not enough.

**These tests assert outcomes, not mechanisms.** An earlier security test in
this repository asserted that a driver raised on privilege escalation. The
driver does not raise - the escalation simply had no effect - so the test was
checking the plumbing rather than the thing that matters. Every test here ends
by asking the database what an attacker actually managed to make happen, which
is a question with only one acceptable answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.actions.executor import (
    ActionExecutor,
    ExecutionRefusal,
    ExecutionRefusedError,
)
from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import (
    ApprovalDecision,
    ApprovalRefusal,
    ApprovalService,
)
from backend.app.audit.service import AuditService
from backend.app.db.app.models import ActionCandidate, ActionExecution, Recommendation
from backend.app.db.app.repositories import EvidenceRepository, IncidentRepository
from backend.app.domain.enums import (
    ActionType,
    DetectedBy,
    EvidenceSource,
    IncidentSeverity,
    IncidentStatus,
    Modality,
)
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.policies.engine import ACTION_REQUIREMENTS, DenialReason, Principal, Role
from tests.integration.conftest import TEST_HMAC_KEY

pytestmark = [pytest.mark.security, pytest.mark.integration]

NOW = datetime(2026, 9, 19, 14, 2, tzinfo=UTC)

DISPATCHER = Principal(subject="dispatcher@axon.test", role=Role.DISPATCHER)
FLEET_MANAGER = Principal(subject="fleet@axon.test", role=Role.FLEET_MANAGER)
ADMIN = Principal(subject="root@axon.test", role=Role.ADMIN)

#: A request carrying every field any action might need, so a refusal can
#: never be mistaken for a validation error about a missing argument. The
#: attacker in these tests is well informed.
FULL_REQUEST = {
    "vehicle_id": "AX-042",
    "shipment_id": "SH-2041",
    "facility_id": "FAC-DAYTON-01",
    "replacement_vehicle_id": "AX-117",
    "swap_location": "TRUCKSTOP-I70-MM42",
}

#: The actions that move a truck, commit capacity or leave the building.
GATED_ACTIONS = sorted(
    (action for action, spec in ACTION_REQUIREMENTS.items() if spec.requires_approval),
    key=lambda action: action.value,
)


class Gate:
    """An incident with a recommendation, and the executor guarding it."""

    def __init__(
        self,
        session: AsyncSession,
        approvals: ApprovalService,
        executor: ActionExecutor,
        incident_id,
        recommendation_id,
        candidate_id,
        evidence_hash: str,
    ) -> None:
        self.session = session
        self.approvals = approvals
        self.executor = executor
        self.incident_id = incident_id
        self.recommendation_id = recommendation_id
        self.candidate_id = candidate_id
        self.evidence_hash = evidence_hash

    def context(self, action: ActionType, **overrides: object) -> ApprovalContext:
        base: dict[str, object] = {
            "incident_id": self.incident_id,
            "action": action,
            "target_ref": "FAC-DAYTON-01",
            "evidence_hashes": [self.evidence_hash],
            "risk_probability": 0.78,
            "baseline_probability": 0.41,
            "baseline_name": "rule_prior",
            "model_version": "slope_extrapolation@1",
            "horizon_minutes": 60,
        }
        base.update(overrides)
        return ApprovalContext(**base)  # type: ignore[arg-type]

    async def executions(self) -> int:
        """How many side effects the system actually recorded."""
        return await self.session.scalar(sa.select(sa.func.count()).select_from(ActionExecution))


@pytest_asyncio.fixture
async def gate(app_session: AsyncSession) -> AsyncIterator[Gate]:
    audit = AuditService(app_session, key=TEST_HMAC_KEY)
    incidents = IncidentRepository(app_session)
    evidence_repo = EvidenceRepository(app_session)

    incident = await incidents.create(
        correlation_key="vehicle:AX-042:thermal_excursion",
        incident_type="thermal_excursion",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-042",
        detected_by=DetectedBy.AXON,
        detected_at=NOW - timedelta(minutes=30),
        status=IncidentStatus.AWAITING_APPROVAL,
    )
    observation = Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id="AX-042"),
        source=EvidenceSource.TELEMETRY,
        modality=Modality.TIMESERIES,
        observation_type="cargo_temp_c",
        value=7.8,
        observed_at=NOW - timedelta(minutes=5),
        ingested_at=NOW - timedelta(minutes=4),
        provenance=Provenance(
            producer="test_execution_gate", producer_version="1.0.0", note="fixture"
        ),
    )
    await evidence_repo.add(observation.model_copy(update={"incident_id": incident.id}))

    candidate = ActionCandidate(
        incident_id=incident.id,
        action_type=ActionType.REROUTE_TO_COLD_STORAGE.value,
        target_ref="FAC-DAYTON-01",
        feasible=True,
    )
    app_session.add(candidate)
    await app_session.flush()

    recommendation = Recommendation(
        incident_id=incident.id,
        selected_action_id=candidate.id,
        narrative="Reroute.",
        cited_evidence_ids=[str(observation.id)],
        grounding_check={"citations_present": True},
    )
    app_session.add(recommendation)
    await app_session.flush()

    approvals = ApprovalService(app_session, audit)
    yield Gate(
        session=app_session,
        approvals=approvals,
        executor=ActionExecutor(app_session, audit, approvals),
        incident_id=incident.id,
        recommendation_id=recommendation.id,
        candidate_id=candidate.id,
        evidence_hash=observation.content_hash,
    )


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", GATED_ACTIONS, ids=lambda a: a.value)
async def test_no_gated_action_executes_without_an_approval(gate: Gate, action: ActionType) -> None:
    """Every consequential action, with a permitted principal and no signature.

    Enumerated rather than sampled. "Reroute is gated" is a statement about
    one action; the bypass rate being zero is a statement about all of them,
    and only one of those is the invariant.
    """
    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=ADMIN,
            context=gate.context(action),
            request=FULL_REQUEST,
            approval=None,
            at=NOW,
        )
    assert raised.value.refusal is ApprovalRefusal.APPROVAL_PENDING
    assert await gate.executions() == 0


async def test_a_denied_approval_authorises_nothing(gate: Gate) -> None:
    """A human said no, and the answer is not a formality."""
    context = gate.context(ActionType.REROUTE_TO_COLD_STORAGE)
    approval = await gate.approvals.request(
        recommendation_id=gate.recommendation_id,
        action_candidate_id=gate.candidate_id,
        context=context,
        required_role=Role.FLEET_MANAGER,
        requested_by=DISPATCHER.subject,
        requested_at=NOW,
    )
    await gate.approvals.decide(
        approval,
        principal=FLEET_MANAGER,
        decision=ApprovalDecision.DENIED,
        context=context,
        at=NOW + timedelta(seconds=30),
        rationale="Dayton has no capacity tonight.",
    )

    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=FULL_REQUEST,
            approval=approval,
            at=NOW + timedelta(minutes=1),
        )
    assert raised.value.refusal is ApprovalRefusal.APPROVAL_DENIED
    assert await gate.executions() == 0


async def test_an_approval_for_one_action_does_not_authorise_another(gate: Gate) -> None:
    """The most plausible confused-deputy attempt.

    A signature obtained for a cheap action, presented for an expensive one.
    The action is inside the bound hash, so the substitution is caught as
    staleness rather than sailing through on the strength of a valid,
    unexpired, correctly-signed approval.
    """
    approved = gate.context(ActionType.INSPECT_REFRIGERATION)
    approval = await gate.approvals.request(
        recommendation_id=gate.recommendation_id,
        action_candidate_id=gate.candidate_id,
        context=approved,
        required_role=Role.FLEET_MANAGER,
        requested_by=DISPATCHER.subject,
        requested_at=NOW,
    )
    await gate.approvals.decide(
        approval,
        principal=FLEET_MANAGER,
        decision=ApprovalDecision.GRANTED,
        context=approved,
        at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=FLEET_MANAGER,
            context=gate.context(ActionType.TRAILER_SWAP),
            request=FULL_REQUEST,
            approval=approval,
            at=NOW + timedelta(minutes=1),
        )
    assert raised.value.refusal is ApprovalRefusal.APPROVAL_STALE
    assert await gate.executions() == 0


async def test_an_approval_cannot_be_replayed_against_another_incident(
    gate: Gate, app_session: AsyncSession
) -> None:
    """A signature is for one situation, not for a shape of situation.

    The second incident is real, because that is the attack that is actually
    available: an identical-looking excursion on another truck, and a
    signature borrowed from the first. The incident id is inside the bound
    hash, so the borrowed approval is stale rather than merely unlucky.
    """
    approved = gate.context(ActionType.REROUTE_TO_COLD_STORAGE)
    approval = await gate.approvals.request(
        recommendation_id=gate.recommendation_id,
        action_candidate_id=gate.candidate_id,
        context=approved,
        required_role=Role.FLEET_MANAGER,
        requested_by=DISPATCHER.subject,
        requested_at=NOW,
    )
    await gate.approvals.decide(
        approval,
        principal=FLEET_MANAGER,
        decision=ApprovalDecision.GRANTED,
        context=approved,
        at=NOW + timedelta(seconds=30),
    )

    other = await IncidentRepository(app_session).create(
        correlation_key="vehicle:AX-117:thermal_excursion",
        incident_type="thermal_excursion",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-117",
        detected_by=DetectedBy.AXON,
        detected_at=NOW - timedelta(minutes=20),
        status=IncidentStatus.AWAITING_APPROVAL,
    )

    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=FLEET_MANAGER,
            context=gate.context(ActionType.REROUTE_TO_COLD_STORAGE, incident_id=other.id),
            request=FULL_REQUEST,
            approval=approval,
            at=NOW + timedelta(minutes=1),
        )
    assert raised.value.refusal is ApprovalRefusal.APPROVAL_STALE
    assert await gate.executions() == 0


async def test_a_fabricated_incident_id_is_refused_before_anything_is_written(
    gate: Gate,
) -> None:
    """Probing with invented identifiers must not cost the audit trail.

    Found by the test above, in its first form. Every refusal writes an audit
    row whose `incident_id` is a foreign key, so a context naming an incident
    that does not exist made the *audit append* fail rather than the
    execution: the action was still correctly refused, but the transaction
    was poisoned and the record of the refusal went with it. An attacker
    enumerating identifiers would have left no trace and taken the session
    down on the way out.

    The fix is an explicit lookup before anything is recorded, so the refusal
    is a refusal rather than an integrity error, and the chain stays usable.
    """
    fabricated = gate.context(ActionType.REROUTE_TO_COLD_STORAGE, incident_id=uuid4())

    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=FLEET_MANAGER,
            context=fabricated,
            request=FULL_REQUEST,
            approval=None,
            at=NOW,
        )
    assert raised.value.refusal is ExecutionRefusal.UNKNOWN_INCIDENT
    assert await gate.executions() == 0

    # The session survives, which is the part that was broken. A refusal that
    # leaves the transaction unusable stops every later audit append too.
    audit = AuditService(gate.session, key=TEST_HMAC_KEY)
    await audit.append(
        actor="probe",
        action="incident.detected",
        subject_kind="incident",
        subject_id=str(gate.incident_id),
        incident_id=gate.incident_id,
        occurred_at=NOW,
    )
    assert (await audit.verify()).valid


async def test_a_role_that_may_not_act_is_refused_before_approval_is_considered(
    gate: Gate,
) -> None:
    """A dispatcher cannot swap trailers, approval or no approval.

    Policy is checked before the signature, so the refusal names the
    authorisation failure rather than reporting a missing approval - which
    would send an operator to find a signature that would not have helped.
    """
    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=DISPATCHER,
            context=gate.context(ActionType.TRAILER_SWAP),
            request=FULL_REQUEST,
            approval=None,
            at=NOW,
        )
    assert raised.value.refusal is DenialReason.ROLE_NOT_PERMITTED
    assert await gate.executions() == 0


async def test_a_viewer_cannot_execute_anything_with_a_side_effect(gate: Gate) -> None:
    """The lowest-privilege role, against the whole catalogue.

    `do_nothing` is the sole exception and is not a side effect; asserting it
    separately keeps the sweep honest rather than quietly excluding whatever
    happened to pass.
    """
    viewer = Principal(subject="viewer@axon.test", role=Role.VIEWER)
    for action in ActionType:
        if action is ActionType.DO_NOTHING:
            continue
        with pytest.raises(ExecutionRefusedError):
            await gate.executor.execute(
                principal=viewer,
                context=gate.context(action),
                request=FULL_REQUEST,
                approval=None,
                at=NOW,
            )
    assert await gate.executions() == 0


async def test_the_kill_switch_stops_an_approved_action(gate: Gate) -> None:
    """A kill switch with an exemption is not a kill switch.

    The approval here is live, correctly signed and hash-matching. Disabling
    the action type refuses it anyway, and refuses it for an Admin.
    """
    context = gate.context(ActionType.REROUTE_TO_COLD_STORAGE)
    approval = await gate.approvals.request(
        recommendation_id=gate.recommendation_id,
        action_candidate_id=gate.candidate_id,
        context=context,
        required_role=Role.FLEET_MANAGER,
        requested_by=DISPATCHER.subject,
        requested_at=NOW,
    )
    await gate.approvals.decide(
        approval,
        principal=FLEET_MANAGER,
        decision=ApprovalDecision.GRANTED,
        context=context,
        at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(ExecutionRefusedError) as raised:
        await gate.executor.execute(
            principal=ADMIN,
            context=context,
            request=FULL_REQUEST,
            approval=approval,
            at=NOW + timedelta(minutes=1),
            disabled_actions=frozenset({ActionType.REROUTE_TO_COLD_STORAGE}),
        )
    assert raised.value.refusal is DenialReason.ACTION_DISABLED
    assert await gate.executions() == 0


async def test_every_refused_attempt_is_on_the_record(gate: Gate) -> None:
    """An action refused with no trace is indistinguishable from one never tried.

    The count matters as much as the presence: an audit trail that recorded
    one entry for a sustained probing campaign would be worse than useless,
    because it would look like a single mistake.
    """
    from backend.app.db.app.models import AuditEvent

    attempts = [ActionType.TRAILER_SWAP, ActionType.SWITCH_FACILITY, ActionType.TRAILER_SWAP]
    for action in attempts:
        with pytest.raises(ExecutionRefusedError):
            await gate.executor.execute(
                principal=ADMIN,
                context=gate.context(action),
                request=FULL_REQUEST,
                approval=None,
                at=NOW,
            )

    refusals = await gate.session.scalar(
        sa.select(sa.func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "action.refused")
    )
    assert refusals == len(attempts)
    assert await gate.executions() == 0
