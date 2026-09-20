"""The simulated executors, and the properties that make them testable.

Nothing here executes anything real. What is being protected is that the
*governance* around execution has something deterministic to act on, and that
an executor cannot quietly invent the part of a request it was not given.
"""

from __future__ import annotations

import pytest

from backend.app.actions.effects import MIN_SLOPE_WINDOW_MINUTES, EffectKind, ExpectedEffect
from backend.app.actions.simulators.base import reference_number
from backend.app.actions.simulators.coldchain import ACTION_SPECS, build_simulators
from backend.app.domain.enums import ActionType

SIMULATORS = build_simulators()

#: A request carrying every field any action might need, so the table-driven
#: tests below can execute all ten without a per-action fixture.
FULL_REQUEST = {
    "vehicle_id": "AX-042",
    "shipment_id": "SH-2041",
    "facility_id": "FAC-DAYTON-01",
    "replacement_vehicle_id": "AX-117",
    "swap_location": "TRUCKSTOP-I70-MM42",
}


def test_every_action_type_has_an_executor() -> None:
    """An action nobody can execute is safe, and silently unreachable.

    The decision engine ranks every `ActionType`, so one missing from this
    table would be recommended and then refused at the last step, which reads
    to an operator as a bug rather than as a policy.
    """
    assert set(ACTION_SPECS) == set(ActionType)
    assert set(SIMULATORS) == set(ActionType)


class TestDeterminism:
    def test_the_same_request_produces_the_same_result(self) -> None:
        """No clock, no `random`, no counter.

        A reference number drawn from `random` would differ on every run, and
        an idempotency test would *still* pass - it returns the stored row -
        so the non-determinism would hide exactly where it is hardest to find.
        """
        simulator = SIMULATORS[ActionType.REROUTE_TO_COLD_STORAGE]
        first = simulator.execute(FULL_REQUEST, idempotency_key="approval:abc")
        second = simulator.execute(FULL_REQUEST, idempotency_key="approval:abc")
        assert first.result == second.result

    def test_a_different_request_produces_a_different_reference(self) -> None:
        simulator = SIMULATORS[ActionType.REROUTE_TO_COLD_STORAGE]
        first = simulator.execute(FULL_REQUEST, idempotency_key="approval:abc")
        second = simulator.execute(FULL_REQUEST, idempotency_key="approval:def")
        assert first.result["reference"] != second.result["reference"]

    def test_a_reference_is_readable_down_a_phone_line(self) -> None:
        """Short and prefixed, so a dispatcher can quote it.

        A raw 64-character digest is unusable by the humans who have to read
        references to each other during an incident.
        """
        reference = reference_number("RRT", "approval:abc")
        assert reference.startswith("RRT-")
        assert len(reference) == len("RRT-") + 10
        assert reference.isupper()


class TestWhatAnExecutorRefusesToInvent:
    @pytest.mark.parametrize(
        ("action", "missing"),
        [
            (ActionType.REROUTE_TO_COLD_STORAGE, "facility_id"),
            (ActionType.TRAILER_SWAP, "swap_location"),
            (ActionType.ESCALATE_MAINTENANCE, "vehicle_id"),
            (ActionType.DRAFT_CUSTOMER_NOTIFICATION, "shipment_id"),
        ],
    )
    def test_a_missing_field_is_a_refusal_not_a_default(
        self, action: ActionType, missing: str
    ) -> None:
        """Invariant I6, applied to actions.

        A reroute defaulted to "the nearest facility" puts a destination
        nobody chose into a record an auditor will later read as a decision.
        """
        request = {key: value for key, value in FULL_REQUEST.items() if key != missing}
        with pytest.raises(ValueError, match=missing):
            SIMULATORS[action].execute(request, idempotency_key="approval:abc")

    def test_the_result_carries_only_fields_the_action_declared(self) -> None:
        """Extra request fields do not leak into the stored result.

        The result is what an auditor reads back. Echoing whatever the caller
        happened to send would make that record depend on the shape of the
        caller rather than on the action.
        """
        outcome = SIMULATORS[ActionType.CONTACT_DRIVER].execute(
            {**FULL_REQUEST, "note": "an unrelated field"}, idempotency_key="k"
        )
        assert "note" not in outcome.result
        assert outcome.result["vehicle_id"] == "AX-042"

    def test_every_result_is_marked_simulated(self) -> None:
        """So no stored execution can be mistaken for a real side effect."""
        for action, simulator in SIMULATORS.items():
            outcome = simulator.execute(FULL_REQUEST, idempotency_key=f"k:{action.value}")
            assert outcome.result["simulated"] is True


class TestTheClaimsActionsMake:
    def test_doing_nothing_is_a_prediction_and_is_graded_like_one(self) -> None:
        """Inaction is not exempt from verification.

        `do_nothing` claims the cargo stays in spec. If that claim fails, the
        incident reopens - which is what stops "we decided it was fine" being
        unfalsifiable.
        """
        spec = ACTION_SPECS[ActionType.DO_NOTHING]
        assert spec.effect is EffectKind.TEMPERATURE_WITHIN_ENVELOPE

    def test_a_maintenance_escalation_only_claims_the_rise_stops(self) -> None:
        """Grading it against a full recovery would fail an escalation that worked.

        A work order does not move cargo. Promising a recovery on its behalf
        and then marking it failed would make the outcome metric a measure of
        how ambitious each action's claim was.
        """
        assert ACTION_SPECS[ActionType.ESCALATE_MAINTENANCE].effect is (
            EffectKind.TEMPERATURE_STOPS_RISING
        )
        assert ACTION_SPECS[ActionType.REROUTE_TO_COLD_STORAGE].effect is (
            EffectKind.TEMPERATURE_WITHIN_ENVELOPE
        )

    def test_a_customer_notification_is_drafted_and_not_sent(self) -> None:
        """The distinction the compliance officer's signature exists to protect.

        A draft is reviewable. A sent message to a pharmaceutical customer
        about a temperature excursion is a regulatory statement that cannot be
        recalled.
        """
        outcome = SIMULATORS[ActionType.DRAFT_CUSTOMER_NOTIFICATION].execute(
            FULL_REQUEST, idempotency_key="k"
        )
        assert "Not sent" in outcome.summary

    def test_every_declared_window_is_long_enough_to_answer_its_own_question(self) -> None:
        """The error the slope measurement caught, kept out by construction.

        The first draft set these windows from operational intuition - a phone
        call resolves in twenty minutes, so twenty minutes - which is a
        statement about how long an intervention takes, not about how long the
        data needs to show it worked. Below ninety minutes a healthy run and a
        failing one produce overlapping slopes, so a twenty-minute verdict was
        a coin toss dressed as a measurement.
        """
        for action, spec in ACTION_SPECS.items():
            if spec.effect is EffectKind.TEMPERATURE_STOPS_RISING:
                assert spec.window_minutes >= MIN_SLOPE_WINDOW_MINUTES, action

    def test_an_unanswerable_claim_cannot_be_constructed_at_all(self) -> None:
        """Refused at construction rather than handled at grading time.

        Accepting the window and returning `inconclusive` for ever would hide
        an unanswerable question inside something that looks like a result.
        """
        with pytest.raises(ValueError, match="at least 90 minutes"):
            ExpectedEffect(kind=EffectKind.TEMPERATURE_STOPS_RISING, within_minutes=20)

    def test_an_unobservable_effect_cannot_carry_a_window(self) -> None:
        """A window implies something will be measured at the end of it."""
        with pytest.raises(ValueError, match="cannot have a verification window"):
            ExpectedEffect(kind=EffectKind.NONE_OBSERVABLE, within_minutes=30)
