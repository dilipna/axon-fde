"""Requesting, granting and validating approvals.

This is invariant I5: *no consequential action executes without a valid,
unexpired, hash-matching approval.* The executor asks this module one
question and refuses on anything but a clean answer.

**Expiry and staleness are different failures, and the distinction is the
whole point.** An approval can be:

- *unexpired and stale* - granted two minutes ago, but three readings have
  landed since and the probability moved from 0.41 to 0.86. The clock says
  fine; the world says re-ask.
- *expired and unchanged* - granted yesterday against a situation that has
  not moved at all. Nothing is wrong with the facts; the authority has simply
  lapsed, and an authority with no expiry is a standing permission.

They have separate codes because they have separate remedies. Stale means
"look again, the answer may have changed". Expired means "the answer is
probably still right, ask for it again anyway". Collapsing them into one
"invalid" would tell an operator nothing about which.

**Every refusal is reported, not just the first.** An approval that is both
expired and stale returns both codes. Reporting only the highest-precedence
one would send an operator to refresh the expiry, obtain a new approval, and
only then discover the evidence had moved too - two round trips through a
human for one situation, during an incident.

**The approver's role comes from the token.** ``decide`` takes a ``Principal``
and checks it against the approval's ``required_role``. Admin is not a
wildcard here, for the same reason it is not one in the policy engine: the
approver is named per action so that an escalation path is a design decision
rather than an accident of enum ordering.

Nothing here commits. An approval is part of the surrounding transaction along
with its audit event, so that a granted approval and the record of the grant
land together or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.approvals.binding import ApprovalContext
from backend.app.audit.service import AuditService
from backend.app.db.app.models import Approval
from backend.app.policies.engine import Principal, Role

__all__ = [
    "DEFAULT_APPROVAL_TTL",
    "ApprovalCheck",
    "ApprovalDecision",
    "ApprovalRefusal",
    "ApprovalRefusedError",
    "ApprovalService",
    "evaluate_approval",
]

#: How long an approval stays good for. Fifteen minutes is longer than a
#: dispatcher needs to act on a decision they just made, and shorter than the
#: time in which a degrading reefer materially changes state - the flagship
#: scenario moves from in-spec to saturated in 51 minutes, so an hour-long
#: approval could be executed against a genuinely different truck.
DEFAULT_APPROVAL_TTL = timedelta(minutes=15)


class ApprovalDecision(StrEnum):
    """What a human said. Stored as the ``decision`` column."""

    GRANTED = "granted"
    DENIED = "denied"


class ApprovalRefusal(StrEnum):
    """Why an approval cannot authorise an execution right now.

    Machine-readable, so refusals can be counted by cause. A rising
    ``APPROVAL_STALE`` rate means the world is moving faster than humans are
    answering, which is an operational finding about the process rather than
    a bug in this module.
    """

    #: Nobody has answered yet.
    APPROVAL_PENDING = "approval_pending"
    #: A human said no. Not an error - a decision.
    APPROVAL_DENIED = "approval_denied"
    #: The authority has lapsed. The facts may be unchanged.
    APPROVAL_EXPIRED = "approval_expired"
    #: The facts have moved since the approval was bound to them.
    APPROVAL_STALE = "approval_stale"
    #: The principal is not the role this action requires sign-off from.
    WRONG_APPROVER = "wrong_approver"
    #: Somebody already answered; a second answer would overwrite the record.
    ALREADY_DECIDED = "already_decided"


#: The order refusals are reported in when more than one applies. Decision
#: state first because it is the most fundamental - an approval nobody granted
#: is not made worse by also being expired. Within that, expiry before
#: staleness, because expiry is cheap to check and cheap to explain.
_REFUSAL_PRECEDENCE: tuple[ApprovalRefusal, ...] = (
    ApprovalRefusal.APPROVAL_DENIED,
    ApprovalRefusal.APPROVAL_PENDING,
    ApprovalRefusal.APPROVAL_EXPIRED,
    ApprovalRefusal.APPROVAL_STALE,
)


class ApprovalRefusedError(Exception):
    """An approval operation was refused, carrying the machine-readable why."""

    def __init__(self, refusal: ApprovalRefusal, detail: str = "") -> None:
        super().__init__(f"{refusal.value}{': ' + detail if detail else ''}")
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """Whether an approval authorises an execution, and every reason it does not."""

    refusals: tuple[ApprovalRefusal, ...] = ()
    #: Which bound fields moved, when the refusal includes ``APPROVAL_STALE``.
    #: Empty otherwise. "The approval is stale" is unactionable; "risk.
    #: probability and evidence_hashes moved" is what an operator can act on.
    changed_fields: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.refusals

    @property
    def reason(self) -> ApprovalRefusal | None:
        """The primary refusal, for a single-value field or a log line."""
        return self.refusals[0] if self.refusals else None

    def describe(self) -> str:
        if self.valid:
            return "valid"
        parts = ", ".join(refusal.value for refusal in self.refusals)
        if self.changed_fields:
            parts += f" (changed: {', '.join(self.changed_fields)})"
        return parts


def evaluate_approval(
    *,
    decision: str | None,
    expires_at: datetime,
    bound_context_hash: str,
    context: ApprovalContext,
    now: datetime,
    granted_context: ApprovalContext | None = None,
) -> ApprovalCheck:
    """The pure predicate behind every approval check.

    Takes primitives rather than an ORM row so the rule can be tested without
    a database, and so the identical rule can run in the executor, in a
    benchmark grader and in an API handler.

    Expiry is inclusive at the boundary: an approval is *not* valid at the
    exact instant it lapses. The other choice leaves a window whose width
    depends on clock resolution, which is not a property anybody should have
    to reason about during an incident.

    ``granted_context`` is what the approver actually saw, when the caller
    still holds it. Given, staleness is reported *field by field*; omitted,
    the refusal is still correct but says only "something moved". The hash on
    its own cannot say what changed, and pretending otherwise would mean
    inventing a diff.
    """
    if now.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError(
            "approval validity needs timezone-aware timestamps; a naive one "
            "compares against the server's local time and would expire "
            "approvals early or late depending on where the process runs"
        )

    found: set[ApprovalRefusal] = set()

    if decision is None:
        found.add(ApprovalRefusal.APPROVAL_PENDING)
    elif decision != ApprovalDecision.GRANTED.value:
        found.add(ApprovalRefusal.APPROVAL_DENIED)

    if now >= expires_at:
        found.add(ApprovalRefusal.APPROVAL_EXPIRED)

    changed: tuple[str, ...] = ()
    if context.bound_hash() != bound_context_hash:
        found.add(ApprovalRefusal.APPROVAL_STALE)
        if granted_context is not None:
            changed = context.differences_from(granted_context)

    return ApprovalCheck(
        refusals=tuple(refusal for refusal in _REFUSAL_PRECEDENCE if refusal in found),
        changed_fields=changed,
    )


class ApprovalService:
    """Requests, grants and validates approvals, writing the audit trail."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditService,
        *,
        ttl: timedelta = DEFAULT_APPROVAL_TTL,
    ) -> None:
        self._session = session
        self._audit = audit
        self._ttl = ttl

    async def request(
        self,
        *,
        recommendation_id: UUID,
        action_candidate_id: UUID,
        context: ApprovalContext,
        required_role: Role,
        requested_by: str,
        requested_at: datetime | None = None,
        ttl: timedelta | None = None,
    ) -> Approval:
        """Open an approval bound to the context the approver will be shown.

        The hash is computed here, once, from the same object that renders the
        approval screen. Computing it separately for display and for binding
        is how the two drift apart, and a binding that covers something other
        than what was displayed is worse than no binding: it passes review.
        """
        moment = requested_at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")

        approval = Approval(
            recommendation_id=recommendation_id,
            action_candidate_id=action_candidate_id,
            bound_context_hash=context.bound_hash(),
            required_role=required_role.value,
            requested_at=moment,
            expires_at=moment + (ttl or self._ttl),
        )
        self._session.add(approval)
        await self._session.flush()

        await self._audit.append(
            actor=requested_by,
            action="approval.requested",
            subject_kind="approval",
            subject_id=str(approval.id),
            incident_id=context.incident_id,
            occurred_at=moment,
            payload={
                "action": context.action.value,
                "target_ref": context.target_ref,
                "required_role": required_role.value,
                "bound_context_hash": approval.bound_context_hash,
                "expires_at": approval.expires_at.isoformat(),
                # The canonical form is stored, not just its hash. An auditor
                # asking "what did they see?" needs the answer, and a hash
                # alone can only confirm a guess they already have.
                "bound_context": context.canonical(),
            },
        )
        return approval

    async def decide(
        self,
        approval: Approval,
        *,
        principal: Principal,
        decision: ApprovalDecision,
        context: ApprovalContext,
        at: datetime | None = None,
        rationale: str | None = None,
    ) -> Approval:
        """Record a human answer, refusing anything that is not a live ask.

        The staleness check runs *here* as well as at execution time, and it
        has to. An approver looking at a screen rendered four minutes ago is
        approving that screen; if the world moved while they read it, granting
        would bind a signature to a situation they were never shown.

        Raises:
            ApprovalRefusedError: Wrong approver, already answered, expired,
                or the context moved since the ask.
        """
        moment = at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("at must be timezone-aware")

        if approval.decision is not None:
            raise ApprovalRefusedError(
                ApprovalRefusal.ALREADY_DECIDED,
                f"answered {approval.decision} by {approval.decided_by} already",
            )

        if principal.role.value != approval.required_role:
            # Refused before anything is recorded on the approval, and audited
            # so the attempt is visible. An authorisation failure that leaves
            # no trace is indistinguishable from one that never happened.
            await self._audit_refusal(
                approval,
                context,
                principal=principal,
                refusal=ApprovalRefusal.WRONG_APPROVER,
                at=moment,
                detail=f"requires {approval.required_role}, principal is {principal.role.value}",
            )
            raise ApprovalRefusedError(
                ApprovalRefusal.WRONG_APPROVER,
                f"{approval.required_role} must answer this, not {principal.role.value}",
            )

        check = evaluate_approval(
            # Deliberately not `approval.decision`: this call is what sets it.
            # Passing the real value would make every grant report itself
            # pending, and the two blocking conditions we care about here are
            # expiry and staleness.
            decision=ApprovalDecision.GRANTED.value,
            expires_at=approval.expires_at,
            bound_context_hash=approval.bound_context_hash,
            context=context,
            now=moment,
        )
        if not check.valid:
            refusal = check.refusals[0]
            await self._audit_refusal(
                approval,
                context,
                principal=principal,
                refusal=refusal,
                at=moment,
                detail=check.describe(),
            )
            raise ApprovalRefusedError(refusal, check.describe())

        approval.decision = decision.value
        approval.decided_by = principal.subject
        approval.decided_at = moment
        approval.rationale = rationale
        await self._session.flush()

        await self._audit.append(
            actor=principal.subject,
            actor_role=principal.role.value,
            action=f"approval.{decision.value}",
            subject_kind="approval",
            subject_id=str(approval.id),
            incident_id=context.incident_id,
            occurred_at=moment,
            payload={
                "action": context.action.value,
                "target_ref": context.target_ref,
                "bound_context_hash": approval.bound_context_hash,
                "rationale": rationale,
            },
        )
        return approval

    def check(
        self,
        approval: Approval,
        context: ApprovalContext,
        *,
        now: datetime | None = None,
        granted_context: ApprovalContext | None = None,
    ) -> ApprovalCheck:
        """Whether this approval authorises acting on this context, right now.

        Synchronous and side-effect free: the executor calls it before doing
        anything, and a check that wrote to the database would make "may I?"
        indistinguishable from "I did".
        """
        return evaluate_approval(
            decision=approval.decision,
            expires_at=approval.expires_at,
            bound_context_hash=approval.bound_context_hash,
            context=context,
            now=now or datetime.now(UTC),
            granted_context=granted_context,
        )

    async def get(self, approval_id: UUID) -> Approval | None:
        result = await self._session.execute(select(Approval).where(Approval.id == approval_id))
        return result.scalars().first()

    async def _audit_refusal(
        self,
        approval: Approval,
        context: ApprovalContext,
        *,
        principal: Principal,
        refusal: ApprovalRefusal,
        at: datetime,
        detail: str,
    ) -> None:
        await self._audit.append(
            actor=principal.subject,
            actor_role=principal.role.value,
            action="approval.refused",
            subject_kind="approval",
            subject_id=str(approval.id),
            incident_id=context.incident_id,
            occurred_at=at,
            payload={
                "refusal": refusal.value,
                "detail": detail,
                "action": context.action.value,
                "bound_context_hash": approval.bound_context_hash,
                "presented_context_hash": context.bound_hash(),
            },
        )
