"""The gate between a recommendation and something happening in the world.

This is where invariant I5 is enforced: *no consequential action executes
without a valid, unexpired, hash-matching approval.* Everything else in the
system produces advice. This module is the only place that acts on it, which
is why the refusal path here is longer than the success path.

**The order of checks is deliberate.**

1. *Idempotency first.* A retry of an execution that already happened returns
   the first result without re-checking policy or approval. That is not a
   shortcut - it is the correct semantics. The action already occurred; the
   approval that authorised it has since expired, and refusing the retry
   would leave the caller unable to learn what its own request did. A retry
   asks "what happened?", not "may I?".
2. *Policy.* Whether this principal may take this action at all.
3. *Approval.* Only for actions the policy engine says need it, and only
   ``may_execute_immediately`` decides which - never ``allowed``, which is
   the field that reads like permission and is not.

**A refusal does not create an ``action_execution`` row.** That table records
things that happened; a refused action did not. Putting refusals in it would
make "how many reroutes did we execute?" a query with a filter people forget,
and the first time somebody forgets it the number is wrong in the direction
of claiming more action than took place. Refusals go to the audit chain,
which is where attempts belong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.actions.effects import ExpectedEffect
from backend.app.actions.simulators.coldchain import SimulatedExecutor, build_simulators
from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import ApprovalCheck, ApprovalRefusal, ApprovalService
from backend.app.audit.service import AuditService
from backend.app.db.app.models import ActionExecution, Approval, Incident
from backend.app.db.app.repositories.execution import ExecutionRepository
from backend.app.domain.enums import ActionType
from backend.app.policies.engine import DenialReason, Principal, evaluate

__all__ = [
    "ActionExecutor",
    "ExecutionOutcome",
    "ExecutionRefusal",
    "ExecutionRefusedError",
    "derive_idempotency_key",
]


class ExecutionRefusal(StrEnum):
    """Refusals that are neither a policy denial nor a problem with a signature."""

    #: The context names an incident that does not exist.
    #:
    #: Checked explicitly, and first, because the alternative is worse than it
    #: looks. Every refusal writes an audit event whose ``incident_id`` is a
    #: foreign key, so a fabricated id makes the *audit append* fail: the
    #: action is still refused, but the surrounding transaction is poisoned
    #: and the record of the refusal is lost. Someone probing with invented
    #: identifiers would leave no trace, which is the one outcome the audit
    #: table exists to prevent.
    UNKNOWN_INCIDENT = "unknown_incident"

    #: No registered executor for the action. Unreachable while the spec
    #: table is complete, and a test asserts that it is.
    NO_EXECUTOR = "no_executor"


#: Anything that can stop an execution. Three enums rather than one because
#: they are answered by three different people: a policy denial is a question
#: for whoever grants roles, an approval refusal for whoever signs, and these
#: for whoever is calling.
Refusal = DenialReason | ApprovalRefusal | ExecutionRefusal


class ExecutionRefusedError(Exception):
    """The action was not carried out, and why in machine-readable form.

    ``refusal`` is a ``DenialReason``, an ``ApprovalRefusal`` or an
    ``ExecutionRefusal``. All are ``StrEnum``, so a caller can log
    ``exc.refusal.value`` without caring which, while a caller that *does*
    care can still branch on the type.
    """

    def __init__(self, refusal: Refusal, detail: str = "") -> None:
        super().__init__(f"{refusal.value}{': ' + detail if detail else ''}")
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What the executor did, or found had already been done."""

    execution: ActionExecution
    #: True when this call returned a previously stored result rather than
    #: causing a side effect. The caller needs this: scheduling outcome
    #: verification twice for one action would open two verification windows
    #: and could reopen an incident that was verified perfectly well.
    replayed: bool
    expected_effect: ExpectedEffect | None
    summary: str

    @property
    def attempts(self) -> int:
        return self.execution.attempts


def derive_idempotency_key(
    *,
    action: ActionType,
    incident_id: UUID,
    approval_id: UUID | None = None,
    target_ref: str | None = None,
) -> str:
    """The default key for an execution.

    **With an approval, the key is the approval.** One approval authorises one
    execution, so a retry carrying the same approval is the same execution.
    A *second* approval for the same action is a separate human decision and
    gets a separate key, which is what makes it a separate execution rather
    than a silently swallowed duplicate - that distinction is the whole reason
    idempotency lives on the execution and not on the approval.

    **Without one**, the key falls back to the incident, the action and the
    target. The consequence is worth stating plainly: two deliberate calls to
    the same driver about the same incident collapse into one execution. For
    the unapproved actions - a phone call, a flag on a record - that is the
    safer failure, and a caller that genuinely wants a second one passes its
    own key.
    """
    if approval_id is not None:
        return f"approval:{approval_id}"
    return f"incident:{incident_id}:{action.value}:{target_ref or '-'}"


class ActionExecutor:
    """Executes actions, or refuses and says why."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditService,
        approvals: ApprovalService,
        *,
        simulators: dict[ActionType, SimulatedExecutor] | None = None,
    ) -> None:
        self._session = session
        self._audit = audit
        self._approvals = approvals
        self._repo = ExecutionRepository(session)
        self._simulators = simulators or build_simulators()

    async def execute(
        self,
        *,
        principal: Principal,
        context: ApprovalContext,
        request: dict[str, Any],
        approval: Approval | None = None,
        idempotency_key: str | None = None,
        at: datetime | None = None,
        disabled_actions: frozenset[ActionType] = frozenset(),
    ) -> ExecutionOutcome:
        """Carry out the approved action, exactly once.

        Raises:
            ExecutionRefusedError: Policy denied the action, or it requires an
                approval that is absent, expired, stale, denied or pending.
        """
        moment = at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("at must be timezone-aware")

        # Before the lock, before the audit, before anything. A refusal
        # writes an audit row keyed to this incident, so an id that does not
        # resolve has to be caught here or it takes the audit trail down with
        # it - see `ExecutionRefusal.UNKNOWN_INCIDENT`.
        if await self._session.get(Incident, context.incident_id) is None:
            raise ExecutionRefusedError(
                ExecutionRefusal.UNKNOWN_INCIDENT,
                f"no incident {context.incident_id}; refusing before anything is recorded",
            )

        action = context.action
        key = idempotency_key or derive_idempotency_key(
            action=action,
            incident_id=context.incident_id,
            approval_id=approval.id if approval else None,
            target_ref=context.target_ref,
        )

        # Taken before the lookup, so two concurrent retries queue instead of
        # one of them hitting the unique constraint mid-transaction.
        await self._repo.lock(key)

        existing = await self._repo.by_idempotency_key(key)
        if existing is not None:
            return await self._replay(existing, context, principal=principal, at=moment)

        decision = evaluate(principal, action, disabled_actions=disabled_actions)
        if not decision.allowed:
            reason = decision.reason or DenialReason.ROLE_NOT_PERMITTED
            await self._audit_refusal(
                context,
                principal=principal,
                refusal=reason.value,
                detail=decision.detail,
                at=moment,
                approval=approval,
            )
            raise ExecutionRefusedError(reason, decision.detail)

        # `may_execute_immediately`, never `allowed`. The two are easy to
        # confuse and the confusion is the bypass I5 exists to prevent.
        if not decision.may_execute_immediately:
            # Raises unless a live, hash-matching approval authorises this.
            await self._require_approval(approval, context, principal=principal, at=moment)

        simulator = self._simulators.get(action)
        if simulator is None:
            # Unreachable while the registry is complete, and a test asserts
            # that. Closed by default for the moment somebody adds an
            # ActionType and forgets the spec table.
            await self._audit_refusal(
                context,
                principal=principal,
                refusal=ExecutionRefusal.NO_EXECUTOR.value,
                detail=f"{action.value} has no registered executor",
                at=moment,
                approval=approval,
            )
            raise ExecutionRefusedError(
                ExecutionRefusal.NO_EXECUTOR, f"{action.value} has no registered executor"
            )

        outcome = simulator.execute(request, idempotency_key=key)
        execution = await self._repo.record(
            incident_id=context.incident_id,
            approval_id=approval.id if approval else None,
            idempotency_key=key,
            action_type=action.value,
            request=outcome.request,
            status="succeeded",
            result=outcome.result,
            executed_at=moment,
        )

        await self._audit.append(
            actor=principal.subject,
            actor_role=principal.role.value,
            action="action.executed",
            subject_kind="action_execution",
            subject_id=str(execution.id),
            incident_id=context.incident_id,
            occurred_at=moment,
            payload={
                "action": action.value,
                "idempotency_key": key,
                "approval_id": str(approval.id) if approval else None,
                "bound_context_hash": context.bound_hash(),
                "result": outcome.result,
                "expected_effect": outcome.expected_effect.as_payload(),
                "summary": outcome.summary,
            },
        )
        return ExecutionOutcome(
            execution=execution,
            replayed=False,
            expected_effect=outcome.expected_effect,
            summary=outcome.summary,
        )

    async def _replay(
        self,
        execution: ActionExecution,
        context: ApprovalContext,
        *,
        principal: Principal,
        at: datetime,
    ) -> ExecutionOutcome:
        """Return the first result, counting the retry.

        No policy check and no approval check. The action already happened;
        re-authorising it would mean a retry could be refused because the
        approval that authorised the original has since expired, leaving the
        caller unable to discover the outcome of its own request. Retrying is
        a read.
        """
        await self._repo.note_repeat_attempt(execution)
        await self._audit.append(
            actor=principal.subject,
            actor_role=principal.role.value,
            action="action.execution_replayed",
            subject_kind="action_execution",
            subject_id=str(execution.id),
            incident_id=context.incident_id,
            occurred_at=at,
            payload={
                "action": execution.action_type,
                "idempotency_key": execution.idempotency_key,
                "attempts": execution.attempts,
                # The original, not a recomputation. If the stored result
                # differed from what a fresh run would produce, this is where
                # that would be visible.
                "result": execution.result,
            },
        )
        return ExecutionOutcome(
            execution=execution,
            replayed=True,
            # Deliberately absent on a replay. The expected effect was
            # declared and its verification scheduled when the action first
            # ran; handing it back would invite a caller to schedule a second
            # verification window for one action.
            expected_effect=None,
            summary=f"already executed as {execution.idempotency_key}",
        )

    async def _require_approval(
        self,
        approval: Approval | None,
        context: ApprovalContext,
        *,
        principal: Principal,
        at: datetime,
    ) -> ApprovalCheck:
        """Refuse unless a live, hash-matching approval authorises this."""
        if approval is None:
            await self._audit_refusal(
                context,
                principal=principal,
                refusal=ApprovalRefusal.APPROVAL_PENDING.value,
                detail="the action requires approval and none was supplied",
                at=at,
                approval=None,
            )
            raise ExecutionRefusedError(
                ApprovalRefusal.APPROVAL_PENDING,
                "the action requires approval and none was supplied",
            )

        check = self._approvals.check(approval, context, now=at)
        if not check.valid:
            refusal = check.refusals[0]
            await self._audit_refusal(
                context,
                principal=principal,
                refusal=refusal.value,
                detail=check.describe(),
                at=at,
                approval=approval,
            )
            raise ExecutionRefusedError(refusal, check.describe())
        return check

    async def _audit_refusal(
        self,
        context: ApprovalContext,
        *,
        principal: Principal,
        refusal: str,
        detail: str,
        at: datetime,
        approval: Approval | None,
    ) -> None:
        """Record the attempt. An action refused with no trace is invisible.

        The subject is the incident rather than an execution, because no
        execution exists - that is the point of the refusal.
        """
        await self._audit.append(
            actor=principal.subject,
            actor_role=principal.role.value,
            action="action.refused",
            subject_kind="incident",
            subject_id=str(context.incident_id),
            incident_id=context.incident_id,
            occurred_at=at,
            payload={
                "action": context.action.value,
                "refusal": refusal,
                "detail": detail,
                "approval_id": str(approval.id) if approval else None,
                "presented_context_hash": context.bound_hash(),
            },
        )
