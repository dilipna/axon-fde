"""The governance core, end to end against a real database.

B1 to B5 produce a recommendation. This is the part that stands between a
recommendation and something happening in the world, and it is the block the
whole compliance story rests on, so these tests exercise the *whole* path -
request, grant, execute, verify - rather than each service in isolation.

Every test here also checks the audit chain still verifies at the end. A
governance control whose record of itself does not survive the control being
exercised is not a governance control.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.actions.executor import (
    ActionExecutor,
    ExecutionRefusedError,
    derive_idempotency_key,
)
from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import (
    ApprovalDecision,
    ApprovalRefusal,
    ApprovalRefusedError,
    ApprovalService,
)
from backend.app.audit.service import AuditService
from backend.app.db.app.models import (
    ActionCandidate,
    ActionExecution,
    Approval,
    AuditEvent,
    Incident,
    Recommendation,
)
from backend.app.db.app.repositories import EvidenceRepository, IncidentRepository
from backend.app.domain.enums import (
    ActionType,
    DetectedBy,
    EvidenceSource,
    IncidentSeverity,
    IncidentStatus,
    Modality,
)
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.incidents.lifecycle import IncidentService
from backend.app.policies.engine import Principal, Role
from backend.app.verification.outcome import Verdict
from backend.app.verification.service import VerificationService
from tests.integration.conftest import TEST_HMAC_KEY

pytestmark = pytest.mark.integration

GRANTED_AT = datetime(2026, 9, 19, 14, 2, tzinfo=UTC)
ENVELOPE = TemperatureEnvelope(minimum_c=2.0, maximum_c=8.0)

DISPATCHER = Principal(subject="dispatcher@axon.test", role=Role.DISPATCHER)
FLEET_MANAGER = Principal(subject="fleet@axon.test", role=Role.FLEET_MANAGER)
COMPLIANCE = Principal(subject="compliance@axon.test", role=Role.COMPLIANCE_OFFICER)

REROUTE_REQUEST = {"vehicle_id": "AX-042", "facility_id": "FAC-DAYTON-01"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class Governance:
    """Everything a test needs, wired the way the application wires it."""

    session: AsyncSession
    audit: AuditService
    approvals: ApprovalService
    executor: ActionExecutor
    verification: VerificationService
    incidents: IncidentService
    incident: Incident
    recommendation_id: UUID
    candidate_id: UUID
    evidence: list[Evidence]

    def context(self, **overrides: object) -> ApprovalContext:
        """The world as an approver was shown it, with one field swapped."""
        base: dict[str, object] = {
            "incident_id": self.incident.id,
            "action": ActionType.REROUTE_TO_COLD_STORAGE,
            "target_ref": "FAC-DAYTON-01",
            "evidence_hashes": [item.content_hash for item in self.evidence],
            "risk_probability": 0.78,
            "baseline_probability": 0.41,
            "baseline_name": "rule_prior",
            "model_version": "slope_extrapolation@1",
            "horizon_minutes": 60,
        }
        base.update(overrides)
        return ApprovalContext(**base)  # type: ignore[arg-type]

    async def granted_approval(self, context: ApprovalContext | None = None) -> Approval:
        """A live, granted approval for a reroute. The common starting point."""
        bound = context or self.context()
        approval = await self.approvals.request(
            recommendation_id=self.recommendation_id,
            action_candidate_id=self.candidate_id,
            context=bound,
            required_role=Role.FLEET_MANAGER,
            requested_by=DISPATCHER.subject,
            requested_at=GRANTED_AT,
        )
        await self.approvals.decide(
            approval,
            principal=FLEET_MANAGER,
            decision=ApprovalDecision.GRANTED,
            context=bound,
            at=GRANTED_AT + timedelta(seconds=30),
            rationale="Cold storage has capacity and the trend is clear.",
        )
        return approval


def temperature(value: float, *, observed_at: datetime, entity_id: str = "AX-042") -> Evidence:
    return Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id=entity_id),
        source=EvidenceSource.TELEMETRY,
        modality=Modality.TIMESERIES,
        observation_type="cargo_temp_c",
        value=value,
        observed_at=observed_at,
        ingested_at=observed_at + timedelta(seconds=5),
        provenance=Provenance(producer="test_governance", producer_version="1.0.0", note="fixture"),
    )


@pytest_asyncio.fixture
async def governance(app_session: AsyncSession) -> AsyncIterator[Governance]:
    """An incident with a recommendation, ready to be approved and acted on."""
    audit = AuditService(app_session, key=TEST_HMAC_KEY)
    incidents_repo = IncidentRepository(app_session)
    evidence_repo = EvidenceRepository(app_session)
    incident_service = IncidentService(incidents_repo, evidence_repo, audit)

    incident = await incidents_repo.create(
        correlation_key="vehicle:AX-042:thermal_excursion",
        incident_type="thermal_excursion",
        severity=IncidentSeverity.SEV2,
        entity_kind="vehicle",
        entity_id="AX-042",
        detected_by=DetectedBy.AXON,
        detected_at=GRANTED_AT - timedelta(minutes=30),
        status=IncidentStatus.AWAITING_APPROVAL,
    )

    evidence = [
        temperature(6.4 + 0.2 * i, observed_at=GRANTED_AT - timedelta(minutes=10 - i))
        for i in range(3)
    ]
    await evidence_repo.add_many(
        [item.model_copy(update={"incident_id": incident.id}) for item in evidence]
    )

    candidate = ActionCandidate(
        incident_id=incident.id,
        action_type=ActionType.REROUTE_TO_COLD_STORAGE.value,
        target_ref="FAC-DAYTON-01",
        feasible=True,
        required_approval_role=Role.FLEET_MANAGER.value,
    )
    app_session.add(candidate)
    await app_session.flush()

    recommendation = Recommendation(
        incident_id=incident.id,
        selected_action_id=candidate.id,
        narrative="Reroute to Dayton cold storage.",
        cited_evidence_ids=[str(item.id) for item in evidence],
        grounding_check={"citations_present": True},
    )
    app_session.add(recommendation)
    await app_session.flush()

    approvals = ApprovalService(app_session, audit)
    yield Governance(
        session=app_session,
        audit=audit,
        approvals=approvals,
        executor=ActionExecutor(app_session, audit, approvals),
        verification=VerificationService(app_session, audit, incident_service),
        incidents=incident_service,
        incident=incident,
        recommendation_id=recommendation.id,
        candidate_id=candidate.id,
        evidence=evidence,
    )


async def audit_actions(session: AsyncSession) -> list[str]:
    """Every audit action recorded so far, in chain order."""
    result = await session.execute(sa.select(AuditEvent.action).order_by(AuditEvent.seq))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Staleness and expiry
# ---------------------------------------------------------------------------


class TestApprovingAgainstAWorldThatMoved:
    async def test_executing_against_mutated_evidence_yields_approval_stale(
        self, governance: Governance
    ) -> None:
        """The B6 acceptance criterion, against a real database.

        The approval is granted, live and correctly signed. A new reading
        landed between the signature and the execution, and that alone is
        enough to refuse.
        """
        approval = await governance.granted_approval()

        extra = temperature(7.9, observed_at=GRANTED_AT + timedelta(minutes=1))
        moved = governance.context(
            evidence_hashes=[item.content_hash for item in governance.evidence]
            + [extra.content_hash]
        )

        with pytest.raises(ExecutionRefusedError) as raised:
            await governance.executor.execute(
                principal=FLEET_MANAGER,
                context=moved,
                request=REROUTE_REQUEST,
                approval=approval,
                at=GRANTED_AT + timedelta(minutes=2),
            )
        assert raised.value.refusal is ApprovalRefusal.APPROVAL_STALE

        # Nothing happened, and the attempt is on the record.
        assert (
            await governance.session.scalar(sa.select(sa.func.count()).select_from(ActionExecution))
            == 0
        )
        assert "action.refused" in await audit_actions(governance.session)
        assert (await governance.audit.verify()).valid

    async def test_a_risk_number_that_moved_refuses_the_same_evidence(
        self, governance: Governance
    ) -> None:
        """Not one observation changed, and the execution is still refused.

        This is the failure the binding exists for: a 40% decision executed
        against an 85% world. A recalibration or a model version bump moves
        the number without touching the evidence, so an approval bound only
        to evidence hashes would sail through.
        """
        approval = await governance.granted_approval(governance.context(risk_probability=0.40))
        with pytest.raises(ExecutionRefusedError) as raised:
            await governance.executor.execute(
                principal=FLEET_MANAGER,
                context=governance.context(risk_probability=0.85),
                request=REROUTE_REQUEST,
                approval=approval,
                at=GRANTED_AT + timedelta(minutes=2),
            )
        assert raised.value.refusal is ApprovalRefusal.APPROVAL_STALE

    async def test_granting_against_a_world_that_moved_is_refused_too(
        self, governance: Governance
    ) -> None:
        """The check runs at signature time, not only at execution time.

        An approver reading a screen rendered four minutes ago is approving
        that screen. If the world moved while they read it, granting would
        bind a signature to a situation they were never shown - and the
        execution would then pass, because the approval was bound to the new
        world rather than the one the human saw.
        """
        shown = governance.context(risk_probability=0.40)
        approval = await governance.approvals.request(
            recommendation_id=governance.recommendation_id,
            action_candidate_id=governance.candidate_id,
            context=shown,
            required_role=Role.FLEET_MANAGER,
            requested_by=DISPATCHER.subject,
            requested_at=GRANTED_AT,
        )

        with pytest.raises(ApprovalRefusedError) as raised:
            await governance.approvals.decide(
                approval,
                principal=FLEET_MANAGER,
                decision=ApprovalDecision.GRANTED,
                context=governance.context(risk_probability=0.85),
                at=GRANTED_AT + timedelta(minutes=4),
            )
        assert raised.value.refusal is ApprovalRefusal.APPROVAL_STALE
        assert approval.decision is None


class TestExpiry:
    async def test_an_expired_approval_is_refused_with_a_distinct_reason(
        self, governance: Governance
    ) -> None:
        """Expiry and staleness are different failures.

        The world here is byte for byte what the approver saw - the same
        evidence, the same probability, the same action. Only the clock moved,
        and the refusal says so specifically rather than reporting a generic
        invalidity.
        """
        approval = await governance.granted_approval()
        context = governance.context()

        with pytest.raises(ExecutionRefusedError) as raised:
            await governance.executor.execute(
                principal=FLEET_MANAGER,
                context=context,
                request=REROUTE_REQUEST,
                approval=approval,
                at=approval.expires_at + timedelta(minutes=1),
            )
        assert raised.value.refusal is ApprovalRefusal.APPROVAL_EXPIRED
        # The binding still matches. Nothing about the situation changed.
        assert context.bound_hash() == approval.bound_context_hash

    async def test_an_approval_expires_even_though_nothing_moved(
        self, governance: Governance
    ) -> None:
        """An authority with no expiry is a standing permission."""
        approval = await governance.granted_approval()
        check = governance.approvals.check(
            approval, governance.context(), now=approval.expires_at + timedelta(hours=8)
        )
        assert check.refusals == (ApprovalRefusal.APPROVAL_EXPIRED,)


class TestWhoMayApprove:
    async def test_a_compliance_officer_cannot_approve_a_reroute(
        self, governance: Governance
    ) -> None:
        """The approver is named per action, and seniority is not a wildcard.

        A compliance officer outranks a dispatcher on customer
        communications and has no standing on where a truck goes.
        """
        context = governance.context()
        approval = await governance.approvals.request(
            recommendation_id=governance.recommendation_id,
            action_candidate_id=governance.candidate_id,
            context=context,
            required_role=Role.FLEET_MANAGER,
            requested_by=DISPATCHER.subject,
            requested_at=GRANTED_AT,
        )
        with pytest.raises(ApprovalRefusedError) as raised:
            await governance.approvals.decide(
                approval,
                principal=COMPLIANCE,
                decision=ApprovalDecision.GRANTED,
                context=context,
                at=GRANTED_AT + timedelta(minutes=1),
            )
        assert raised.value.refusal is ApprovalRefusal.WRONG_APPROVER
        assert approval.decision is None
        assert "approval.refused" in await audit_actions(governance.session)

    async def test_an_admin_cannot_approve_a_reroute_either(self, governance: Governance) -> None:
        """Deny is absolute, and that includes the approval step.

        A privilege that can be escalated in an emergency is a privilege that
        will be escalated during the incident where it matters most.
        """
        context = governance.context()
        approval = await governance.approvals.request(
            recommendation_id=governance.recommendation_id,
            action_candidate_id=governance.candidate_id,
            context=context,
            required_role=Role.FLEET_MANAGER,
            requested_by=DISPATCHER.subject,
            requested_at=GRANTED_AT,
        )
        with pytest.raises(ApprovalRefusedError):
            await governance.approvals.decide(
                approval,
                principal=Principal(subject="root@axon.test", role=Role.ADMIN),
                decision=ApprovalDecision.GRANTED,
                context=context,
                at=GRANTED_AT + timedelta(minutes=1),
            )

    async def test_an_approval_cannot_be_answered_twice(self, governance: Governance) -> None:
        """A second answer would overwrite the record of the first."""
        approval = await governance.granted_approval()
        with pytest.raises(ApprovalRefusedError) as raised:
            await governance.approvals.decide(
                approval,
                principal=FLEET_MANAGER,
                decision=ApprovalDecision.DENIED,
                context=governance.context(),
                at=GRANTED_AT + timedelta(minutes=1),
            )
        assert raised.value.refusal is ApprovalRefusal.ALREADY_DECIDED
        assert approval.decision == ApprovalDecision.GRANTED.value


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestExecution:
    async def test_a_granted_approval_executes(self, governance: Governance) -> None:
        approval = await governance.granted_approval()
        outcome = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=governance.context(),
            request=REROUTE_REQUEST,
            approval=approval,
            at=GRANTED_AT + timedelta(minutes=1),
        )
        assert outcome.replayed is False
        assert outcome.execution.status == "succeeded"
        assert outcome.execution.result is not None
        assert outcome.execution.result["facility_id"] == "FAC-DAYTON-01"
        assert "action.executed" in await audit_actions(governance.session)
        assert (await governance.audit.verify()).valid

    async def test_re_executing_the_same_approval_is_a_no_op(self, governance: Governance) -> None:
        """The acceptance criterion: a retry returns the first result.

        Not "succeeds again" - the same row, the same reference, the same
        timestamp. A retried reroute that dispatched a second truck would be
        the most expensive possible way to handle a dropped connection.
        """
        approval = await governance.granted_approval()
        context = governance.context()

        first = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=REROUTE_REQUEST,
            approval=approval,
            at=GRANTED_AT + timedelta(minutes=1),
        )
        second = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=REROUTE_REQUEST,
            approval=approval,
            at=GRANTED_AT + timedelta(minutes=3),
        )

        assert second.replayed is True
        assert second.execution.id == first.execution.id
        assert second.execution.result == first.execution.result
        assert second.attempts == 2
        # The original time, not the retry's. An auditor asks when the reroute
        # happened, not when somebody last asked about it.
        assert second.execution.executed_at == GRANTED_AT + timedelta(minutes=1)
        assert (
            await governance.session.scalar(sa.select(sa.func.count()).select_from(ActionExecution))
            == 1
        )

    async def test_a_retry_after_the_approval_expired_still_returns_the_result(
        self, governance: Governance
    ) -> None:
        """A retry asks "what happened?", not "may I?".

        Refusing it would leave a caller unable to discover the outcome of its
        own request - and the action already happened, so refusing changes
        nothing except what the caller knows.
        """
        approval = await governance.granted_approval()
        context = governance.context()
        first = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=REROUTE_REQUEST,
            approval=approval,
            at=GRANTED_AT + timedelta(minutes=1),
        )
        replay = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=REROUTE_REQUEST,
            approval=approval,
            at=approval.expires_at + timedelta(hours=2),
        )
        assert replay.replayed is True
        assert replay.execution.id == first.execution.id

    async def test_a_second_approval_is_a_separate_execution(self, governance: Governance) -> None:
        """Idempotency lives on the execution, not on the approval.

        A human signing a second time has made a second decision, and
        collapsing it into the first would lose the record of a deliberate
        repeat intervention.
        """
        context = governance.context()
        first_approval = await governance.granted_approval(context)
        await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=REROUTE_REQUEST,
            approval=first_approval,
            at=GRANTED_AT + timedelta(minutes=1),
        )

        second_approval = await governance.granted_approval(context)
        second = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=context,
            request=REROUTE_REQUEST,
            approval=second_approval,
            at=GRANTED_AT + timedelta(minutes=5),
        )

        assert second.replayed is False
        assert (
            await governance.session.scalar(sa.select(sa.func.count()).select_from(ActionExecution))
            == 2
        )
        assert derive_idempotency_key(
            action=ActionType.REROUTE_TO_COLD_STORAGE,
            incident_id=governance.incident.id,
            approval_id=first_approval.id,
        ) != derive_idempotency_key(
            action=ActionType.REROUTE_TO_COLD_STORAGE,
            incident_id=governance.incident.id,
            approval_id=second_approval.id,
        )

    async def test_an_action_needing_no_approval_executes_without_one(
        self, governance: Governance
    ) -> None:
        """Not everything is gated. A phone call is reversible and cheap."""
        outcome = await governance.executor.execute(
            principal=DISPATCHER,
            context=governance.context(action=ActionType.CONTACT_DRIVER, target_ref="AX-042"),
            request={"vehicle_id": "AX-042"},
            at=GRANTED_AT + timedelta(minutes=1),
        )
        assert outcome.replayed is False
        assert outcome.execution.action_type == ActionType.CONTACT_DRIVER.value


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerificationClosesTheLoop:
    async def _execute_reroute(self, governance: Governance):
        approval = await governance.granted_approval()
        outcome = await governance.executor.execute(
            principal=FLEET_MANAGER,
            context=governance.context(),
            request=REROUTE_REQUEST,
            approval=approval,
            at=GRANTED_AT + timedelta(minutes=1),
        )
        assert outcome.expected_effect is not None
        verification = await governance.verification.schedule(
            incident=governance.incident,
            execution=outcome.execution,
            effect=outcome.expected_effect,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT + timedelta(minutes=1),
        )
        return outcome, verification

    async def test_scheduling_a_verification_moves_the_incident_to_verifying(
        self, governance: Governance
    ) -> None:
        await governance.incidents.transition(
            governance.incident,
            IncidentStatus.ACTING,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT,
        )
        _, verification = await self._execute_reroute(governance)
        assert governance.incident.status == IncidentStatus.VERIFYING.value
        assert verification.verdict is None

    async def test_a_failed_verification_reopens_the_incident(self, governance: Governance) -> None:
        """The acceptance criterion, and the reason the loop is closed at all.

        Without this the only path out of an action is resolution, and the
        resolution rate becomes a count of interventions attempted rather than
        of problems fixed.
        """
        await governance.incidents.transition(
            governance.incident,
            IncidentStatus.ACTING,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT,
        )
        _, verification = await self._execute_reroute(governance)

        window_start = verification.window_start
        still_hot = [
            temperature(9.0 + 0.01 * i, observed_at=window_start + timedelta(minutes=i))
            for i in range(0, 90, 10)
        ]

        outcome = await governance.verification.verify(
            verification,
            incident=governance.incident,
            evidence=still_hot,
            envelope=ENVELOPE,
            at=verification.window_end + timedelta(minutes=1),
        )

        assert outcome.result.verdict is Verdict.FAILED
        assert outcome.reopened is True
        assert outcome.incident_status is IncidentStatus.INVESTIGATING
        assert governance.incident.status == IncidentStatus.INVESTIGATING.value
        assert verification.follow_up_required is True
        assert (await governance.audit.verify()).valid

    async def test_a_confirmed_verification_resolves_the_incident(
        self, governance: Governance
    ) -> None:
        await governance.incidents.transition(
            governance.incident,
            IncidentStatus.ACTING,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT,
        )
        _, verification = await self._execute_reroute(governance)

        window_start = verification.window_start
        recovered = [
            temperature(7.6 - 0.05 * i, observed_at=window_start + timedelta(minutes=i * 10))
            for i in range(9)
        ]

        outcome = await governance.verification.verify(
            verification,
            incident=governance.incident,
            evidence=recovered,
            envelope=ENVELOPE,
            at=verification.window_end + timedelta(minutes=1),
        )
        assert outcome.result.verdict is Verdict.CONFIRMED
        assert outcome.reopened is False
        assert governance.incident.status == IncidentStatus.RESOLVED.value

    async def test_a_verification_with_no_readings_leaves_the_incident_alone(
        self, governance: Governance
    ) -> None:
        """Missing data resolves nothing and reopens nothing.

        Moving the incident either way would be a conclusion drawn from an
        absence, which is exactly what invariant I6 forbids.
        """
        await governance.incidents.transition(
            governance.incident,
            IncidentStatus.ACTING,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT,
        )
        _, verification = await self._execute_reroute(governance)

        outcome = await governance.verification.verify(
            verification,
            incident=governance.incident,
            evidence=[],
            envelope=ENVELOPE,
            at=verification.window_end + timedelta(minutes=1),
        )
        assert outcome.result.verdict is Verdict.INCONCLUSIVE
        assert outcome.incident_status is IncidentStatus.VERIFYING
        assert governance.incident.status == IncidentStatus.VERIFYING.value
        assert verification.follow_up_required is True

    async def test_a_verdict_cannot_be_revised(self, governance: Governance) -> None:
        """An outcome that can be changed quietly is not evidence."""
        await governance.incidents.transition(
            governance.incident,
            IncidentStatus.ACTING,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT,
        )
        _, verification = await self._execute_reroute(governance)
        await governance.verification.verify(
            verification,
            incident=governance.incident,
            evidence=[],
            envelope=ENVELOPE,
            at=verification.window_end + timedelta(minutes=1),
        )
        with pytest.raises(ValueError, match="already concluded"):
            await governance.verification.verify(
                verification,
                incident=governance.incident,
                evidence=[],
                envelope=ENVELOPE,
                at=verification.window_end + timedelta(minutes=2),
            )

    async def test_only_closed_windows_are_due(self, governance: Governance) -> None:
        await governance.incidents.transition(
            governance.incident,
            IncidentStatus.ACTING,
            actor=FLEET_MANAGER.subject,
            at=GRANTED_AT,
        )
        _, verification = await self._execute_reroute(governance)

        assert (
            await governance.verification.due(verification.window_end - timedelta(minutes=1)) == []
        )
        due = await governance.verification.due(verification.window_end)
        assert [item.id for item in due] == [verification.id]


# ---------------------------------------------------------------------------
# The record of all of it
# ---------------------------------------------------------------------------


async def test_every_transition_appears_in_the_audit_chain(governance: Governance) -> None:
    """The whole loop, and a chain that still verifies at the end of it.

    This is the compliance product: an auditor asking "why did you divert
    rather than continue?" gets the request, the signature, the execution and
    the outcome, in order, with each event hashing the one before it.
    """
    await governance.incidents.transition(
        governance.incident,
        IncidentStatus.ACTING,
        actor=FLEET_MANAGER.subject,
        at=GRANTED_AT,
    )
    approval = await governance.granted_approval()
    outcome = await governance.executor.execute(
        principal=FLEET_MANAGER,
        context=governance.context(),
        request=REROUTE_REQUEST,
        approval=approval,
        at=GRANTED_AT + timedelta(minutes=1),
    )
    assert outcome.expected_effect is not None
    verification = await governance.verification.schedule(
        incident=governance.incident,
        execution=outcome.execution,
        effect=outcome.expected_effect,
        actor=FLEET_MANAGER.subject,
        at=GRANTED_AT + timedelta(minutes=1),
    )
    await governance.verification.verify(
        verification,
        incident=governance.incident,
        evidence=[
            temperature(7.4, observed_at=verification.window_start + timedelta(minutes=i * 10))
            for i in range(9)
        ],
        envelope=ENVELOPE,
        at=verification.window_end + timedelta(minutes=1),
    )

    actions = await audit_actions(governance.session)
    assert actions == [
        "incident.status_changed",  # -> acting
        "approval.requested",
        "approval.granted",
        "action.executed",
        "incident.status_changed",  # -> verifying
        "verification.scheduled",
        "incident.status_changed",  # -> resolved
        "verification.completed",
    ]

    report = await governance.audit.verify()
    assert report.valid
    assert report.breaks == ()


async def test_the_approval_record_holds_what_the_approver_saw(
    governance: Governance,
) -> None:
    """A hash alone can only confirm a guess somebody already has.

    An auditor asking "what was in front of them?" needs the answer, so the
    canonical context is stored alongside the hash rather than only the hash.
    """
    context = governance.context()
    await governance.granted_approval(context)

    payload = await governance.session.scalar(
        sa.select(AuditEvent.payload).where(AuditEvent.action == "approval.requested")
    )
    assert payload is not None
    assert payload["bound_context"]["risk"]["probability"] == 0.78
    assert payload["bound_context"]["risk"]["baseline_probability"] == 0.41
    assert payload["bound_context_hash"] == context.bound_hash()
