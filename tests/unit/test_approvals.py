"""Expiry and staleness, which are different failures.

The distinction these tests protect: an approval can be unexpired and stale,
or expired while describing a world that has not moved at all. Two reasons,
two codes, two remedies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import (
    ApprovalDecision,
    ApprovalRefusal,
    evaluate_approval,
)
from backend.app.domain.enums import ActionType

GRANTED_AT = datetime(2026, 9, 19, 14, 2, tzinfo=UTC)
EXPIRES_AT = GRANTED_AT + timedelta(minutes=15)
INCIDENT = uuid4()


def context(**overrides: object) -> ApprovalContext:
    base: dict[str, object] = {
        "incident_id": INCIDENT,
        "action": ActionType.REROUTE_TO_COLD_STORAGE,
        "target_ref": "FAC-DAYTON-01",
        "evidence_hashes": ["aa" * 32, "bb" * 32],
        "risk_probability": 0.78,
        "baseline_probability": 0.41,
        "baseline_name": "rule_prior",
        "model_version": "slope_extrapolation@1",
        "horizon_minutes": 60,
    }
    base.update(overrides)
    return ApprovalContext(**base)  # type: ignore[arg-type]


def check(
    *,
    presented: ApprovalContext | None = None,
    granted: ApprovalContext | None = None,
    decision: str | None = ApprovalDecision.GRANTED.value,
    now: datetime = GRANTED_AT + timedelta(minutes=2),
):
    """Evaluate an approval granted against `granted`, presented with `presented`."""
    bound = granted or context()
    return evaluate_approval(
        decision=decision,
        expires_at=EXPIRES_AT,
        bound_context_hash=bound.bound_hash(),
        context=presented or bound,
        now=now,
        granted_context=bound,
    )


class TestTheHappyPath:
    def test_a_granted_unexpired_matching_approval_is_valid(self) -> None:
        result = check()
        assert result.valid
        assert result.reason is None


class TestStaleness:
    """The world moved. The clock is irrelevant."""

    def test_approving_against_mutated_evidence_yields_approval_stale(self) -> None:
        """A new reading landed between the grant and the execution.

        This is the acceptance criterion for B6, stated as the behaviour
        rather than as a call to a function.
        """
        result = check(
            granted=context(evidence_hashes=["aa" * 32, "bb" * 32]),
            presented=context(evidence_hashes=["aa" * 32, "bb" * 32, "dd" * 32]),
        )
        assert result.refusals == (ApprovalRefusal.APPROVAL_STALE,)
        assert result.changed_fields == ("evidence_hashes",)

    def test_a_risk_number_that_moved_makes_an_approval_stale(self) -> None:
        """The 40%-decision-against-an-85%-world case, end to end.

        Nothing about the evidence set changed - a recalibration moved the
        number on the same observations - and the approval is still refused.
        """
        result = check(
            granted=context(risk_probability=0.40),
            presented=context(risk_probability=0.85),
        )
        assert result.reason is ApprovalRefusal.APPROVAL_STALE
        assert result.changed_fields == ("risk.probability",)

    def test_an_unexpired_approval_can_still_be_stale(self) -> None:
        """Staleness is not a slow form of expiry.

        Two minutes into a fifteen-minute life, with plenty of clock left,
        and the approval is refused anyway.
        """
        result = check(
            granted=context(risk_probability=0.40),
            presented=context(risk_probability=0.85),
            now=GRANTED_AT + timedelta(minutes=2),
        )
        assert ApprovalRefusal.APPROVAL_EXPIRED not in result.refusals
        assert ApprovalRefusal.APPROVAL_STALE in result.refusals

    def test_staleness_without_the_granted_context_still_refuses(self) -> None:
        """The hash alone settles *whether*; only the context settles *what*.

        A caller that no longer holds what the approver saw gets a correct
        refusal and an empty diff, rather than an invented one.
        """
        result = evaluate_approval(
            decision=ApprovalDecision.GRANTED.value,
            expires_at=EXPIRES_AT,
            bound_context_hash=context(risk_probability=0.40).bound_hash(),
            context=context(risk_probability=0.85),
            now=GRANTED_AT + timedelta(minutes=2),
        )
        assert result.reason is ApprovalRefusal.APPROVAL_STALE
        assert result.changed_fields == ()


class TestExpiry:
    """The clock ran out. The world may be exactly as it was."""

    def test_an_expired_approval_is_refused_with_a_distinct_reason(self) -> None:
        """Nothing has changed except the time, and that is enough.

        The refusal code differs from `APPROVAL_STALE` because the remedy
        differs: the answer is probably still right, and it still has to be
        given again.
        """
        result = check(now=EXPIRES_AT + timedelta(minutes=1))
        assert result.refusals == (ApprovalRefusal.APPROVAL_EXPIRED,)
        assert ApprovalRefusal.APPROVAL_STALE not in result.refusals

    def test_an_approval_is_not_valid_at_the_instant_it_lapses(self) -> None:
        """Inclusive at the boundary.

        The alternative leaves a window whose width is however precise the
        clock happens to be, which is not a property anybody should have to
        reason about mid-incident.
        """
        assert check(now=EXPIRES_AT - timedelta(microseconds=1)).valid
        assert not check(now=EXPIRES_AT).valid

    def test_an_expired_and_stale_approval_reports_both(self) -> None:
        """One situation, one round trip.

        Reporting only the higher-precedence refusal would send an operator
        to obtain a fresh approval and *only then* discover the evidence had
        moved too - two interruptions of a human during an incident, for one
        problem.
        """
        result = check(
            granted=context(risk_probability=0.40),
            presented=context(risk_probability=0.85),
            now=EXPIRES_AT + timedelta(minutes=1),
        )
        assert set(result.refusals) == {
            ApprovalRefusal.APPROVAL_EXPIRED,
            ApprovalRefusal.APPROVAL_STALE,
        }
        # Precedence is defined, so the primary reason is not arbitrary.
        assert result.reason is ApprovalRefusal.APPROVAL_EXPIRED


class TestDecisionState:
    def test_an_unanswered_approval_authorises_nothing(self) -> None:
        result = check(decision=None)
        assert result.reason is ApprovalRefusal.APPROVAL_PENDING

    def test_a_denial_is_a_decision_not_an_error(self) -> None:
        result = check(decision=ApprovalDecision.DENIED.value)
        assert result.reason is ApprovalRefusal.APPROVAL_DENIED

    def test_a_denied_approval_that_is_also_stale_reports_the_denial_first(self) -> None:
        """Decision state outranks everything.

        An approval nobody granted is not made more interesting by the world
        having moved since.
        """
        result = check(
            decision=ApprovalDecision.DENIED.value,
            granted=context(risk_probability=0.40),
            presented=context(risk_probability=0.85),
        )
        assert result.reason is ApprovalRefusal.APPROVAL_DENIED
        assert ApprovalRefusal.APPROVAL_STALE in result.refusals


def test_a_naive_timestamp_is_refused_rather_than_guessed_at() -> None:
    """A naive datetime would expire approvals early or late by timezone.

    The same approval would be live in Dublin and expired in Sydney, which is
    a governance control that depends on where the process happens to run.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_approval(
            decision=ApprovalDecision.GRANTED.value,
            expires_at=EXPIRES_AT,
            bound_context_hash=context().bound_hash(),
            context=context(),
            now=datetime(2026, 9, 19, 14, 4),  # noqa: DTZ001 - the point of the test
        )
