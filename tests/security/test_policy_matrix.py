"""The policy matrix, tested cell by cell.

In `tests/security` rather than `tests/unit` because this is a hard gate: the
bypass rate must be zero, and CI runs this suite as a blocking step. A policy
engine that is 95% correct is a policy engine with a hole in it.

Every cell of Role x ActionType is exercised. That is 50 combinations, which
is few enough to enumerate and too many to reason about informally — and
enumerating them is the only way to know that "Admin cannot execute a denied
action" is true of every action rather than the one that got tested.
"""

from __future__ import annotations

import pytest

from backend.app.domain.enums import ActionType
from backend.app.policies.engine import (
    ACTION_REQUIREMENTS,
    DenialReason,
    Principal,
    Role,
    evaluate,
    required_role_for,
)

pytestmark = pytest.mark.security

ALL_CELLS = [(role, action) for role in Role for action in ActionType]


def principal(role: Role) -> Principal:
    return Principal(subject=f"{role.value}@example.test", role=role)


# ---------------------------------------------------------------------------
# The matrix is complete and total
# ---------------------------------------------------------------------------


def test_every_action_has_a_policy_entry() -> None:
    """A missing entry is denied by default, which is safe but unreachable.

    An action nobody can ever take is a feature that silently does not exist,
    and it would be found by a user rather than by CI.
    """
    missing = set(ActionType) - set(ACTION_REQUIREMENTS)
    assert not missing, f"actions with no policy entry: {sorted(a.value for a in missing)}"


def test_the_matrix_has_no_entries_for_actions_that_do_not_exist() -> None:
    assert not set(ACTION_REQUIREMENTS) - set(ActionType)


@pytest.mark.parametrize(("role", "action"), ALL_CELLS)
def test_every_cell_returns_a_decision(role: Role, action: ActionType) -> None:
    """No combination raises, and none returns an ambiguous answer.

    The engine is called on the incident path; an exception here would be an
    outage during exactly the minutes it matters.
    """
    decision = evaluate(principal(role), action)
    assert isinstance(decision.allowed, bool)
    assert isinstance(decision.requires_approval, bool)
    if not decision.allowed:
        assert decision.reason is not None
        assert decision.detail


# ---------------------------------------------------------------------------
# Deny is absolute
# ---------------------------------------------------------------------------


def test_admin_cannot_execute_an_action_it_is_not_permitted() -> None:
    """The bypass this engine exists to refuse.

    A privilege that can be escalated in an emergency is one that will be
    escalated during the incident where it matters most, and the audit record
    would then name the role rather than the control that failed.

    Admin is permitted every action *by the matrix* — but by being listed,
    not by being Admin. This test pins the distinction: remove Admin from a
    row and Admin is denied that row.
    """
    from dataclasses import replace

    original = ACTION_REQUIREMENTS[ActionType.TRAILER_SWAP]
    narrowed = replace(original, allowed_roles=frozenset({Role.FLEET_MANAGER}))
    ACTION_REQUIREMENTS[ActionType.TRAILER_SWAP] = narrowed
    try:
        decision = evaluate(principal(Role.ADMIN), ActionType.TRAILER_SWAP)
        assert not decision.allowed
        assert decision.reason is DenialReason.ROLE_NOT_PERMITTED
    finally:
        ACTION_REQUIREMENTS[ActionType.TRAILER_SWAP] = original


@pytest.mark.parametrize("role", list(Role))
def test_a_disabled_action_is_denied_to_every_role_including_admin(role: Role) -> None:
    """A kill switch with an exemption is not a kill switch.

    Checked before role permission for exactly this reason: an operator taking
    an action type out of service must not discover that one role still has
    it.
    """
    decision = evaluate(
        principal(role),
        ActionType.REROUTE_TO_COLD_STORAGE,
        disabled_actions=frozenset({ActionType.REROUTE_TO_COLD_STORAGE}),
    )
    assert not decision.allowed
    assert decision.reason is DenialReason.ACTION_DISABLED
    assert not decision.may_execute_immediately


def test_a_viewer_can_take_no_action_that_changes_anything() -> None:
    """Read-only means read-only.

    `DO_NOTHING` is the single exception and is not an action in any
    meaningful sense — it is the null option that every expected-value
    comparison has to include.
    """
    for action in ActionType:
        decision = evaluate(principal(Role.VIEWER), action)
        if action is ActionType.DO_NOTHING:
            assert decision.allowed
            continue
        assert not decision.allowed, f"a viewer must not be able to {action.value}"
        assert decision.reason is DenialReason.ROLE_NOT_PERMITTED


def test_a_denied_decision_never_reports_itself_as_executable() -> None:
    """`allowed` and `may_execute_immediately` must not disagree."""
    for role, action in ALL_CELLS:
        decision = evaluate(principal(role), action)
        if not decision.allowed:
            assert not decision.may_execute_immediately


# ---------------------------------------------------------------------------
# Approval is separate from permission
# ---------------------------------------------------------------------------


def test_permission_alone_never_authorises_a_consequential_action() -> None:
    """Invariant I5 at the policy layer.

    A caller that checked `allowed` and executed would skip approval. Every
    consequential action must therefore come back permitted *and* pending.
    """
    consequential = {
        ActionType.REROUTE_TO_COLD_STORAGE,
        ActionType.SWITCH_FACILITY,
        ActionType.TRAILER_SWAP,
        ActionType.ESCALATE_MAINTENANCE,
        ActionType.DRAFT_CUSTOMER_NOTIFICATION,
    }
    for action in consequential:
        requirement = ACTION_REQUIREMENTS[action]
        assert requirement.requires_approval, f"{action.value} must require approval"
        assert requirement.approver_role is not None, f"{action.value} must name its approver"

        for role in requirement.allowed_roles:
            decision = evaluate(principal(role), action)
            assert decision.allowed
            assert decision.requires_approval
            assert not decision.may_execute_immediately, (
                f"{role.value} would execute {action.value} without approval"
            )


def test_a_dispatcher_may_request_a_reroute_but_not_execute_one() -> None:
    """The flagship scenario's action, and the distinction that makes it safe."""
    decision = evaluate(principal(Role.DISPATCHER), ActionType.REROUTE_TO_COLD_STORAGE)
    assert decision.allowed
    assert decision.requires_approval
    assert decision.approver_role is Role.FLEET_MANAGER
    assert not decision.may_execute_immediately


def test_the_customer_notification_is_not_a_dispatcher_decision() -> None:
    """A message to a pharma customer about an excursion is a regulatory
    statement before it is an operational one, and it is read as an
    admission. It needs compliance, not dispatch."""
    assert not evaluate(principal(Role.DISPATCHER), ActionType.DRAFT_CUSTOMER_NOTIFICATION).allowed
    assert required_role_for(ActionType.DRAFT_CUSTOMER_NOTIFICATION) is Role.COMPLIANCE_OFFICER


def test_reversible_actions_do_not_require_approval() -> None:
    """Gating a phone call would slow every incident for no safety gain.

    Approval is a scarce resource: requiring it everywhere trains people to
    grant it without reading, which is how the control stops working.
    """
    for action in (
        ActionType.DO_NOTHING,
        ActionType.CONTACT_DRIVER,
        ActionType.INSPECT_REFRIGERATION,
        ActionType.MARK_FOR_INSPECTION,
        ActionType.CONTINUE_ROUTE,
    ):
        assert not ACTION_REQUIREMENTS[action].requires_approval


def test_do_nothing_is_available_to_everyone() -> None:
    """A system that cannot recommend inaction has a broken prior.

    The null option has to be evaluable by whoever is looking at the incident,
    or the expected-value comparison silently loses its baseline.
    """
    for role in Role:
        assert evaluate(principal(role), ActionType.DO_NOTHING).allowed


# ---------------------------------------------------------------------------
# The engine is pure and unreachable from a model
# ---------------------------------------------------------------------------


def test_the_same_request_always_gets_the_same_answer() -> None:
    """Purity is what makes the matrix testable and the decision auditable."""
    for role, action in ALL_CELLS:
        first = evaluate(principal(role), action)
        second = evaluate(principal(role), action)
        assert first == second


def test_an_action_outside_the_catalogue_cannot_be_expressed() -> None:
    """Prompt injection can at most cause a *proposal*, never a decision.

    The model emits an `ActionType`, which is a closed enum: an invented
    action fails to parse before it reaches policy at all.
    """
    with pytest.raises(ValueError, match="not a valid ActionType"):
        ActionType("exfiltrate_customer_list")


def test_a_principal_must_have_a_subject() -> None:
    """An unattributed decision is not auditable."""
    with pytest.raises(ValueError, match="subject"):
        Principal(subject="", role=Role.ADMIN)


def test_denials_carry_a_machine_readable_reason() -> None:
    """So denials can be counted by cause.

    A rising `role_not_permitted` rate is either an authorisation bug or
    somebody probing, and those are worth telling apart without grepping log
    messages.
    """
    decision = evaluate(principal(Role.VIEWER), ActionType.TRAILER_SWAP)
    assert decision.reason is DenialReason.ROLE_NOT_PERMITTED
    assert "viewer" in decision.detail
    assert "fleet_manager" in decision.detail


def test_the_bypass_rate_across_the_whole_matrix_is_zero() -> None:
    """The gate, stated as the number that must be zero.

    A bypass is any cell where a principal could execute an action
    immediately that the matrix does not permit them to take, or that
    requires approval.
    """
    bypasses = [
        (role.value, action.value)
        for role, action in ALL_CELLS
        if evaluate(principal(role), action).may_execute_immediately
        and (
            role not in ACTION_REQUIREMENTS[action].allowed_roles
            or ACTION_REQUIREMENTS[action].requires_approval
        )
    ]
    assert bypasses == [], f"policy bypasses found: {bypasses}"
