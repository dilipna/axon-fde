"""The policy engine: who may do what, decided by a pure function.

**Deny is absolute.** There is no override, no break-glass, and no Admin
bypass. An Admin can change configuration and read everything; an Admin cannot
execute a reroute that policy forbids. A privilege that can be escalated in an
emergency is a privilege that will be escalated during the incident where it
matters most, and the audit record would then say "Admin" where it should say
which control failed.

**The engine is pure** — no I/O, no clock, no LLM (invariant I9, enforced by
an import-linter contract). It takes a request and returns a decision. That is
what makes the full matrix testable cell by cell, and it is why the policy
cannot be influenced by anything a model emits: the model proposes an
``ActionType`` from a closed enum, and this function decides whether that
action is permitted for that principal. Prompt injection can at most cause a
*proposal*; it cannot reach the decision.

**Roles come from a verified token, never from a request body.** That is
threat T5, and it is enforced at the API boundary. This module takes a
``Principal`` and trusts it, because by the time a principal exists its role
has already been verified — but see ``PolicyDecision.reason``: every denial
records which rule fired, so an authorisation bug is visible in the audit
trail rather than silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.app.domain.enums import ActionType

__all__ = [
    "ACTION_REQUIREMENTS",
    "ActionRequirement",
    "DenialReason",
    "PolicyDecision",
    "Principal",
    "Role",
    "evaluate",
    "required_role_for",
]


class Role(StrEnum):
    """The closed set of roles the system recognises.

    Ordered by breadth of operational authority, but **not** treated as a
    hierarchy in code. A hierarchy invites "Admin implies everything", which
    is the bypass this engine exists to refuse. Each role's permissions are
    listed explicitly below.
    """

    VIEWER = "viewer"
    DISPATCHER = "dispatcher"
    FLEET_MANAGER = "fleet_manager"
    COMPLIANCE_OFFICER = "compliance_officer"
    ADMIN = "admin"


class DenialReason(StrEnum):
    """Why a request was refused.

    A machine-readable reason rather than a message, so denials can be counted
    by cause. A rising ``ROLE_NOT_PERMITTED`` rate means either an
    authorisation bug or somebody probing, and those are worth telling apart.
    """

    ROLE_NOT_PERMITTED = "role_not_permitted"
    APPROVAL_REQUIRED = "approval_required"
    UNKNOWN_ACTION = "unknown_action"
    ACTION_DISABLED = "action_disabled"


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking. The role is already verified by the time this exists."""

    subject: str
    role: Role

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("a principal must have a subject")


@dataclass(frozen=True, slots=True)
class ActionRequirement:
    """What an action costs in authority.

    ``requires_approval`` is separate from ``allowed_roles`` on purpose. A
    dispatcher may *request* a reroute and may not *execute* one unapproved;
    collapsing the two would make the approval step look like a permission
    problem, and the ``APPROVAL_STALE`` path in B6 would have nowhere to live.
    """

    allowed_roles: frozenset[Role]
    requires_approval: bool
    #: Which role must sign off, when approval is required. Deliberately not
    #: "anyone more senior": the approver is named per action so that an
    #: escalation path is a design decision rather than an accident of
    #: enum ordering.
    approver_role: Role | None = None
    description: str = ""


#: The full matrix. Every member of ``ActionType`` appears exactly once, and a
#: test asserts that - an action missing from here would be denied with
#: ``UNKNOWN_ACTION``, which is safe, but silently unreachable, which is not.
ACTION_REQUIREMENTS: dict[ActionType, ActionRequirement] = {
    # -- No side effects. Anyone who can see the incident can do these. -----
    ActionType.DO_NOTHING: ActionRequirement(
        allowed_roles=frozenset(Role),
        requires_approval=False,
        description="Always available, and always costed alongside the rest.",
    ),
    ActionType.CONTINUE_ROUTE: ActionRequirement(
        allowed_roles=frozenset(
            {Role.DISPATCHER, Role.FLEET_MANAGER, Role.COMPLIANCE_OFFICER, Role.ADMIN}
        ),
        requires_approval=False,
        description="Affirming the current plan changes nothing about the world.",
    ),
    # -- Communication and inspection. Reversible, low cost. ---------------
    ActionType.CONTACT_DRIVER: ActionRequirement(
        allowed_roles=frozenset(
            {Role.DISPATCHER, Role.FLEET_MANAGER, Role.COMPLIANCE_OFFICER, Role.ADMIN}
        ),
        requires_approval=False,
        description="A phone call. Reversible and cheap; gating it would slow every incident.",
    ),
    ActionType.INSPECT_REFRIGERATION: ActionRequirement(
        allowed_roles=frozenset({Role.DISPATCHER, Role.FLEET_MANAGER, Role.ADMIN}),
        requires_approval=False,
        description="Asking the driver to look at the unit costs minutes, not money.",
    ),
    ActionType.MARK_FOR_INSPECTION: ActionRequirement(
        allowed_roles=frozenset(
            {Role.DISPATCHER, Role.FLEET_MANAGER, Role.COMPLIANCE_OFFICER, Role.ADMIN}
        ),
        requires_approval=False,
        description="A flag on a record, undone by removing the flag.",
    ),
    # -- Consequential. These move a truck or cost real money. -------------
    ActionType.REROUTE_TO_COLD_STORAGE: ActionRequirement(
        allowed_roles=frozenset({Role.DISPATCHER, Role.FLEET_MANAGER, Role.ADMIN}),
        requires_approval=True,
        approver_role=Role.FLEET_MANAGER,
        description="Changes the destination of a loaded vehicle. Expensive and visible.",
    ),
    ActionType.SWITCH_FACILITY: ActionRequirement(
        allowed_roles=frozenset({Role.FLEET_MANAGER, Role.ADMIN}),
        requires_approval=True,
        approver_role=Role.FLEET_MANAGER,
        description="Commits capacity at another site, which is a commercial decision.",
    ),
    ActionType.TRAILER_SWAP: ActionRequirement(
        allowed_roles=frozenset({Role.FLEET_MANAGER, Role.ADMIN}),
        requires_approval=True,
        approver_role=Role.FLEET_MANAGER,
        description="Takes two vehicles out of service and moves cargo between them.",
    ),
    ActionType.ESCALATE_MAINTENANCE: ActionRequirement(
        allowed_roles=frozenset({Role.DISPATCHER, Role.FLEET_MANAGER, Role.ADMIN}),
        requires_approval=True,
        approver_role=Role.FLEET_MANAGER,
        description="Books engineering time and may take the vehicle off the road.",
    ),
    # -- Leaves the building. -----------------------------------------------
    ActionType.DRAFT_CUSTOMER_NOTIFICATION: ActionRequirement(
        # Not the dispatcher. A message to a pharmaceutical customer about a
        # temperature excursion is a regulatory statement before it is an
        # operational one, and it is read as an admission.
        allowed_roles=frozenset({Role.COMPLIANCE_OFFICER, Role.FLEET_MANAGER, Role.ADMIN}),
        requires_approval=True,
        approver_role=Role.COMPLIANCE_OFFICER,
        description="Communication to a customer about a possible excursion.",
    ),
}


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The answer, and why.

    ``allowed`` and ``requires_approval`` are both present because they are
    different questions. An action can be permitted for this role *and* still
    require sign-off before it executes; treating "allowed" as "go ahead"
    would be the bypass invariant I5 exists to prevent.
    """

    allowed: bool
    requires_approval: bool
    approver_role: Role | None = None
    reason: DenialReason | None = None
    detail: str = ""

    @property
    def may_execute_immediately(self) -> bool:
        """The only question the executor should ever ask.

        Deliberately not `allowed`. A caller that checked `allowed` and then
        executed would skip approval entirely, and the two fields are easy to
        confuse — so the safe reading is given a name.
        """
        return self.allowed and not self.requires_approval

    def describe(self) -> str:
        if self.allowed:
            if self.requires_approval:
                approver = self.approver_role.value if self.approver_role else "an approver"
                return f"permitted, pending approval by {approver}"
            return "permitted"
        return f"denied: {self.reason.value if self.reason else 'unspecified'} - {self.detail}"


def required_role_for(action: ActionType) -> Role | None:
    """Which role must approve this action, if any."""
    requirement = ACTION_REQUIREMENTS.get(action)
    return requirement.approver_role if requirement else None


def evaluate(
    principal: Principal,
    action: ActionType,
    *,
    disabled_actions: frozenset[ActionType] = frozenset(),
) -> PolicyDecision:
    """Decide whether this principal may take this action.

    Pure. Given the same arguments it returns the same decision, forever,
    which is what makes the matrix exhaustively testable and what lets the
    same function run in the API, in the workflow and in a benchmark grader
    without three implementations drifting apart.

    ``disabled_actions`` is the kill switch: an operator can take an action
    type out of service globally without a deploy. It is checked *before*
    role permission, so disabling an action denies it to everyone including
    Admin — a kill switch with an exemption is not a kill switch.
    """
    requirement = ACTION_REQUIREMENTS.get(action)
    if requirement is None:
        # Unreachable while the matrix is complete, and a test asserts that it
        # is. Kept because closed-by-default is the correct behaviour for the
        # moment somebody adds an ActionType and forgets this file.
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reason=DenialReason.UNKNOWN_ACTION,
            detail=f"{action.value} has no policy entry; refusing by default",
        )

    if action in disabled_actions:
        return PolicyDecision(
            allowed=False,
            requires_approval=False,
            reason=DenialReason.ACTION_DISABLED,
            detail=f"{action.value} is disabled for all roles",
        )

    if principal.role not in requirement.allowed_roles:
        permitted = ", ".join(sorted(role.value for role in requirement.allowed_roles))
        return PolicyDecision(
            allowed=False,
            requires_approval=requirement.requires_approval,
            approver_role=requirement.approver_role,
            reason=DenialReason.ROLE_NOT_PERMITTED,
            detail=f"{principal.role.value} may not {action.value}; permitted roles: {permitted}",
        )

    return PolicyDecision(
        allowed=True,
        requires_approval=requirement.requires_approval,
        approver_role=requirement.approver_role,
    )
