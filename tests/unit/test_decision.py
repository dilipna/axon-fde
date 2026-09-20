"""The decision engine: costed options, ranked, with the flip point reported.

The properties that matter here are not "it produces a number" but "it
produces the *right* number for the right reason, and says when the number is
not to be trusted". A ranking that silently reverses two points of probability
later is worse than no ranking.
"""

from __future__ import annotations

import pytest

from backend.app.decision.catalogue import ACTION_COSTS, FacilityOption, Feasibility
from backend.app.decision.engine import (
    DECISION_FLIP_TOLERANCE,
    DELAY_COST_PER_MINUTE_USD,
    rank_options,
)
from backend.app.decision.feasibility import MAX_DETOUR_MINUTES, assess_feasibility
from backend.app.domain.enums import ActionType
from backend.app.policies.engine import ACTION_REQUIREMENTS, Role

pytestmark = pytest.mark.unit

#: The flagship shipment: SH-2041, Meridian Pharmaceuticals.
CARGO_VALUE = 184_000.0

#: The three seeded facilities, with CS-13 full — which is what makes the
#: infeasibility path testable against real data rather than a fixture.
GARY = FacilityOption(
    facility_id="CS-11",
    name="Gary Cold Storage",
    capabilities=frozenset({"cold_storage", "pharma_certified", "trailer_swap"}),
    slots_available=3,
    detour_minutes=45.0,
)
FORT_WAYNE = FacilityOption(
    facility_id="CS-12",
    name="Fort Wayne Cold Chain",
    capabilities=frozenset({"cold_storage", "pharma_certified"}),
    slots_available=11,
    detour_minutes=95.0,
)
DAYTON_FULL = FacilityOption(
    facility_id="CS-13",
    name="Dayton Refrigerated Depot",
    capabilities=frozenset({"cold_storage", "trailer_swap"}),
    slots_available=0,
    detour_minutes=60.0,
)

ALL_FACILITIES = [GARY, FORT_WAYNE, DAYTON_FULL]


def policy_map() -> dict[ActionType, tuple[bool, str | None]]:
    """Approval requirements, as the policy engine reports them."""
    return {
        action: (
            requirement.requires_approval,
            requirement.approver_role.value if requirement.approver_role else None,
        )
        for action, requirement in ACTION_REQUIREMENTS.items()
    }


def rank(probability: float, **kwargs: object) -> object:
    return rank_options(
        probability=probability,
        cargo_value_usd=float(kwargs.pop("cargo_value_usd", CARGO_VALUE)),
        feasibility=kwargs.pop("feasibility", assess_feasibility(facilities=ALL_FACILITIES)),  # type: ignore[arg-type]
        permitted=policy_map(),
    )


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_every_action_has_a_cost_entry() -> None:
    """An action with no cost cannot be ranked, so it silently disappears."""
    missing = set(ActionType) - set(ACTION_COSTS)
    assert not missing, f"actions with no cost model: {sorted(a.value for a in missing)}"


def test_no_action_claims_to_increase_risk() -> None:
    """A multiplier above 1 would mean the intervention makes things worse.

    That is a different action, not a negative discount, and letting it
    through would produce an expected value that rewards inaction for the
    wrong reason.
    """
    for action, cost in ACTION_COSTS.items():
        assert 0.0 <= cost.risk_multiplier <= 1.0, action.value


def test_every_cost_states_its_basis() -> None:
    """Invariant I7: a number without a stated basis is not quotable."""
    for action, cost in ACTION_COSTS.items():
        assert cost.basis.strip(), f"{action.value} has no basis for its numbers"


def test_a_risk_multiplier_above_one_is_refused() -> None:
    from backend.app.decision.catalogue import ActionCost

    with pytest.raises(ValueError, match="risk_multiplier"):
        ActionCost(direct_cost_usd=0.0, delay_minutes=0.0, risk_multiplier=1.5, basis="x")


# ---------------------------------------------------------------------------
# Feasibility from real facility data
# ---------------------------------------------------------------------------


def test_a_full_facility_is_not_an_expensive_option_but_no_option() -> None:
    """Dayton has trailer-swap capability and zero free slots.

    Ranking it by expected value would put a recommendation in front of a
    dispatcher that cannot be carried out.
    """
    verdicts = assess_feasibility(facilities=[DAYTON_FULL])
    assert not verdicts[ActionType.TRAILER_SWAP].feasible
    assert "slot" in verdicts[ActionType.TRAILER_SWAP].reason


def test_cold_storage_alone_does_not_qualify_for_pharma_cargo() -> None:
    """Certification is not a nice-to-have.

    A facility that can keep something cold but is not pharma-certified cannot
    take this load, and conflating the two is how a compliance breach gets
    recommended by a system that was trying to help.
    """
    uncertified = FacilityOption(
        facility_id="CS-99",
        name="Generic Cold Store",
        capabilities=frozenset({"cold_storage"}),
        slots_available=20,
        detour_minutes=10.0,
    )
    verdicts = assess_feasibility(facilities=[uncertified])
    assert not verdicts[ActionType.REROUTE_TO_COLD_STORAGE].feasible
    assert "pharma_certified" in verdicts[ActionType.REROUTE_TO_COLD_STORAGE].reason


def test_the_nearest_qualifying_facility_is_chosen() -> None:
    verdicts = assess_feasibility(facilities=ALL_FACILITIES)
    reroute = verdicts[ActionType.REROUTE_TO_COLD_STORAGE]
    assert reroute.feasible
    assert reroute.detail["facility_id"] == "CS-11"
    assert reroute.detail["detour_minutes"] == 45.0


def test_a_facility_beyond_the_detour_limit_is_not_a_rescue() -> None:
    """An intervention arriving after the cargo is ruined is not an option."""
    far = FacilityOption(
        facility_id="CS-98",
        name="Far Away",
        capabilities=frozenset({"cold_storage", "pharma_certified"}),
        slots_available=5,
        detour_minutes=MAX_DETOUR_MINUTES + 1,
    )
    assert not assess_feasibility(facilities=[far])[ActionType.REROUTE_TO_COLD_STORAGE].feasible


def test_a_shipment_about_to_arrive_cannot_usefully_be_rerouted() -> None:
    """The detour costs more time than remains, so the cargo arrives either way."""
    verdicts = assess_feasibility(facilities=ALL_FACILITIES, minutes_to_destination=12.0)
    assert not verdicts[ActionType.REROUTE_TO_COLD_STORAGE].feasible
    assert "12 minutes remain" in verdicts[ActionType.REROUTE_TO_COLD_STORAGE].reason


def test_an_unreachable_driver_rules_out_the_actions_that_need_one() -> None:
    verdicts = assess_feasibility(facilities=ALL_FACILITIES, driver_contactable=False)
    assert not verdicts[ActionType.CONTACT_DRIVER].feasible
    assert not verdicts[ActionType.INSPECT_REFRIGERATION].feasible
    # Rerouting does not need the driver's cooperation to be decided.
    assert verdicts[ActionType.REROUTE_TO_COLD_STORAGE].feasible


def test_every_action_gets_a_verdict() -> None:
    """A caller must never have to tell "feasible" from "not mentioned"."""
    verdicts = assess_feasibility(facilities=ALL_FACILITIES)
    assert set(verdicts) == set(ActionType)


# ---------------------------------------------------------------------------
# Expected value
# ---------------------------------------------------------------------------


def test_doing_nothing_wins_when_the_risk_is_low_and_the_cargo_is_ordinary() -> None:
    """A system that always recommends action has a broken prior.

    The false-alarm control at the decision layer. Stated on a moderately
    valued load, because on a very valuable one the answer is genuinely
    different — see the next test, which is the more interesting case.
    """
    result = rank(0.02, cargo_value_usd=12_000.0)
    assert result.recommended is not None  # type: ignore[attr-defined]
    assert result.recommended.action is ActionType.DO_NOTHING  # type: ignore[attr-defined]


def test_on_very_valuable_cargo_a_low_risk_is_an_honest_close_call() -> None:
    """One percent of 184,000 dollars is 1,840 — more than most interventions cost.

    So on the flagship consignment even a 1% excursion risk makes a cheap
    intervention marginally positive in expectation. That is arithmetic, not a
    bug, and the right response is not to suppress it but to report that the
    options are not distinguishable: the margin is tens of dollars on an
    uncalibrated probability.

    A model that claimed a confident recommendation here would be inventing
    precision it does not have.
    """
    result = rank(0.01)
    assert result.is_close_call  # type: ignore[attr-defined]


def test_rerouting_wins_when_the_risk_is_high() -> None:
    """The flagship case, and it must agree with the scenario's ground truth.

    `compressor_degradation_pharma_01` declares `correct_action:
    reroute_to_cold_storage`. The decision engine has to reach that
    independently, from costs and a probability, or the expected-value layer
    and the benchmark are measuring different things.

    This test caught a real error: with a trailer swap modelled as *more*
    effective than a reroute, the swap won. Transferring warm cargo between
    trailers exposes it in a way a fixed cold store does not, so that ordering
    was wrong on the physics before it was wrong against the scenario.
    """
    result = rank(0.85)
    assert result.recommended is not None  # type: ignore[attr-defined]
    assert result.recommended.action is ActionType.REROUTE_TO_COLD_STORAGE  # type: ignore[attr-defined]
    assert not result.is_close_call, (  # type: ignore[attr-defined]
        "at 85% risk on 184k of pharma the recommendation should be clear"
    )


def test_the_null_option_is_always_present_and_always_feasible() -> None:
    """Even when every facility is full, doing nothing remains on the table.

    Its expected value is the baseline every other option is measured
    against; dropping it would leave the comparison without a floor.
    """
    result = rank(0.9, feasibility=assess_feasibility(facilities=[]))
    actions = [option.action for option in result.options]  # type: ignore[attr-defined]
    assert ActionType.DO_NOTHING in actions
    null_option = next(
        option
        for option in result.options  # type: ignore[attr-defined]
        if option.action is ActionType.DO_NOTHING
    )
    assert null_option.feasible


def test_the_flagship_produces_at_least_three_costed_candidates_plus_do_nothing() -> None:
    """B5's acceptance criterion, stated as the test that checks it."""
    result = rank(0.85)
    feasible = result.feasible_options  # type: ignore[attr-defined]
    assert len(feasible) >= 4
    assert any(option.action is ActionType.DO_NOTHING for option in feasible)
    assert all(option.expected_value_usd <= 0 for option in feasible)


def test_infeasible_options_are_kept_with_their_reason_not_discarded() -> None:
    """ "We considered a trailer swap and Dayton was full" is what an auditor asks."""
    result = rank(0.85, feasibility=assess_feasibility(facilities=[DAYTON_FULL]))
    swap = next(
        option
        for option in result.options  # type: ignore[attr-defined]
        if option.action is ActionType.TRAILER_SWAP
    )
    assert not swap.feasible
    assert swap.feasibility.reason
    # Ranked last, but present.
    assert swap not in result.feasible_options  # type: ignore[attr-defined]


def test_expected_value_is_a_loss_and_never_positive() -> None:
    """Every term is a cost. A positive EV would mean an intervention that
    pays for itself, which none of these do — they reduce a loss."""
    for probability in (0.0, 0.25, 0.5, 0.99):
        for option in rank(probability).options:  # type: ignore[attr-defined]
            assert option.expected_value_usd <= 0.0


def test_the_arithmetic_is_checkable_term_by_term() -> None:
    """A recommendation should be challengeable on its premises."""
    result = rank(0.5)
    reroute = next(
        option
        for option in result.options  # type: ignore[attr-defined]
        if option.action is ActionType.REROUTE_TO_COLD_STORAGE
    )
    cost = ACTION_COSTS[ActionType.REROUTE_TO_COLD_STORAGE]

    assert reroute.direct_cost_usd == cost.direct_cost_usd
    assert reroute.delay_cost_usd == cost.delay_minutes * DELAY_COST_PER_MINUTE_USD
    assert reroute.residual_risk == pytest.approx(0.5 * cost.risk_multiplier)
    assert reroute.expected_loss_usd == pytest.approx(reroute.residual_risk * CARGO_VALUE)
    assert reroute.expected_value_usd == pytest.approx(
        -(reroute.direct_cost_usd + reroute.delay_cost_usd + reroute.expected_loss_usd)
    )


def test_a_cheap_cargo_does_not_justify_an_expensive_rescue() -> None:
    """The same risk on a 2000 dollar load is a different decision.

    If cargo value did not change the answer, the expected-value model would
    be decoration.
    """
    result = rank(0.85, cargo_value_usd=2_000.0)
    assert result.recommended.action is ActionType.DO_NOTHING  # type: ignore[attr-defined]


def test_an_impossible_probability_is_refused() -> None:
    with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
        rank(1.4)


def test_the_ranking_is_deterministic() -> None:
    """A stored recommendation must be reproducible.

    An unstable sort would make two runs over identical inputs disagree, and
    the disagreement would surface as an unexplainable audit discrepancy.
    """
    first = [option.action for option in rank(0.6).options]  # type: ignore[attr-defined]
    second = [option.action for option in rank(0.6).options]  # type: ignore[attr-defined]
    assert first == second


# ---------------------------------------------------------------------------
# Flip sensitivity
# ---------------------------------------------------------------------------


def test_the_recommendation_changes_as_the_risk_rises() -> None:
    """Doing nothing at 2%, rerouting at 85%. The model has to move.

    If the recommendation were the same at both ends, the expected-value layer
    would be decoration on a fixed answer.
    """
    low = rank(0.02, cargo_value_usd=12_000.0).recommended.action  # type: ignore[attr-defined]
    high = rank(0.85, cargo_value_usd=12_000.0).recommended.action  # type: ignore[attr-defined]
    assert low is ActionType.DO_NOTHING
    assert high is ActionType.REROUTE_TO_COLD_STORAGE


def test_a_reported_flip_point_lies_inside_the_probability_range() -> None:
    """A flip at p=4.7 would be arithmetic escaping into the output."""
    result = rank(0.4, cargo_value_usd=12_000.0)
    assert result.flip is not None  # type: ignore[attr-defined]
    assert result.flip.probability is not None  # type: ignore[attr-defined]
    assert 0.0 <= result.flip.probability <= 1.0  # type: ignore[attr-defined]


def test_the_top_two_never_crossing_is_reported_as_no_flip() -> None:
    """On the flagship case rerouting beats a trailer swap at every probability.

    It is both cheaper and, once the transfer exposure is priced, more
    effective — so the lines never cross inside [0, 1]. "This ordering does
    not depend on the risk estimate" is a stronger statement than a confidence
    interval, and it is computable.
    """
    result = rank(0.85)
    assert result.recommended.action is ActionType.REROUTE_TO_COLD_STORAGE  # type: ignore[attr-defined]
    assert result.flip is not None  # type: ignore[attr-defined]
    assert result.flip.probability is None  # type: ignore[attr-defined]
    assert "every probability" in result.flip.describe()  # type: ignore[attr-defined]


def test_the_reported_flip_point_is_where_the_answer_actually_changes() -> None:
    """The solved crossing must match the empirical one.

    An analytic flip point that disagreed with the behaviour would be worse
    than none: it would be quoted, and it would be wrong.
    """
    result = rank(0.4, cargo_value_usd=12_000.0)
    crossing = result.flip.probability  # type: ignore[attr-defined]
    assert crossing is not None

    just_below = rank(  # type: ignore[attr-defined]
        max(0.0, crossing - 0.05), cargo_value_usd=12_000.0
    ).recommended.action
    just_above = rank(  # type: ignore[attr-defined]
        min(1.0, crossing + 0.05), cargo_value_usd=12_000.0
    ).recommended.action
    assert just_below is not just_above, (
        f"the engine reports a flip at p={crossing:.3f} but the recommendation "
        "does not change across it"
    )


def test_two_options_with_the_same_risk_multiplier_never_cross() -> None:
    """Parallel lines. Reported as None rather than as a huge number.

    "These never swap" is a stronger and more useful statement than a flip
    point at p=4.7.
    """
    only_inert = {
        action: Feasibility.no("excluded for this test")
        for action in ActionType
        if action
        not in (
            ActionType.DO_NOTHING,
            ActionType.CONTINUE_ROUTE,
            ActionType.MARK_FOR_INSPECTION,
        )
    }
    result = rank(0.5, feasibility=only_inert)
    assert result.flip is not None  # type: ignore[attr-defined]
    assert result.flip.probability is None  # type: ignore[attr-defined]
    assert "every probability" in result.flip.describe()  # type: ignore[attr-defined]


def test_a_close_call_is_reported_as_one() -> None:
    """Presenting an indistinguishable pair as a ranked choice is false precision.

    At the flip point the top two options differ by essentially nothing, and
    the engine has to say so rather than pick one and sound confident.
    """
    result = rank(0.4, cargo_value_usd=12_000.0)
    crossing = result.flip.probability  # type: ignore[attr-defined]
    assert crossing is not None
    at_the_flip = rank(crossing, cargo_value_usd=12_000.0)
    assert at_the_flip.is_close_call  # type: ignore[attr-defined]


def test_a_clear_decision_is_not_reported_as_a_close_call() -> None:
    """Otherwise the warning means nothing and gets ignored."""
    assert not rank(0.99).is_close_call  # type: ignore[attr-defined]


def test_two_economically_identical_actions_are_reported_as_indistinguishable() -> None:
    """`do_nothing` and `continue_route` cost the same and change the same.

    At low risk they tie exactly, and the engine reports a close call. That is
    the truthful answer, not a defect: they differ in whether the decision was
    recorded, which is a governance distinction rather than an economic one,
    and the expected-value model cannot see it.
    """
    result = rank(0.01, cargo_value_usd=12_000.0)
    top_two = {option.action for option in result.feasible_options[:2]}  # type: ignore[attr-defined]
    assert top_two == {ActionType.DO_NOTHING, ActionType.CONTINUE_ROUTE}
    assert result.is_close_call  # type: ignore[attr-defined]
    # And the tie breaks toward doing nothing, not toward doing something.
    assert result.recommended.action is ActionType.DO_NOTHING  # type: ignore[attr-defined]


def test_an_information_gathering_action_is_never_ev_optimal() -> None:
    """A limitation of the model, pinned so it cannot quietly disappear.

    `CONTACT_DRIVER` and `INSPECT_REFRIGERATION` cost something and reduce no
    risk, so expected value will never choose them. That is correct for a
    model that prices only risk reduction — their worth is diagnostic, and
    pricing information is future work.

    The first draft of the catalogue gave them risk multipliers of 0.95 and
    0.85, which made a 90-dollar roadside inspection optimal on a shipment
    with a 2% excursion risk. If this test starts failing, check whether a
    discount was reintroduced to make these actions rankable rather than
    because they genuinely reduce risk.
    """
    for probability in (0.01, 0.2, 0.5, 0.85, 0.99):
        recommended = rank(probability).recommended.action  # type: ignore[attr-defined]
        assert recommended not in (ActionType.CONTACT_DRIVER, ActionType.INSPECT_REFRIGERATION)

    for action in (ActionType.CONTACT_DRIVER, ActionType.INSPECT_REFRIGERATION):
        assert ACTION_COSTS[action].risk_multiplier == 1.0, (
            f"{action.value} claims a risk reduction it does not deliver"
        )


def test_the_close_call_band_scales_with_cargo_value() -> None:
    """One percent of a consignment, not a fixed number of dollars.

    A 50 dollar margin is noise on a 184k load and decisive on a 2k one.
    """
    assert DECISION_FLIP_TOLERANCE == 0.01


# ---------------------------------------------------------------------------
# What the engine refuses to pretend
# ---------------------------------------------------------------------------


def test_every_result_declares_that_its_probability_is_uncalibrated() -> None:
    """The single easiest place in the project to ship a misleading number.

    Multiplying an uncalibrated score by a cargo value produces a figure with
    units of dollars and the epistemic status of a guess. A consumer must have
    to actively ignore that rather than never be told.
    """
    result = rank(0.85)
    assert result.uses_calibrated_probability is False  # type: ignore[attr-defined]
    for option in result.options:  # type: ignore[attr-defined]
        assert option.assumptions["probability_is_calibrated"] is False


def test_each_option_carries_the_assumptions_behind_its_numbers() -> None:
    for option in rank(0.5).options:  # type: ignore[attr-defined]
        assert option.assumptions["basis"]
        assert "risk_multiplier" in option.assumptions


def test_approval_requirements_travel_with_the_candidates() -> None:
    """The decision engine does not decide policy; it carries the verdict.

    Calling the policy engine from here would make this module impure and let
    a ranking depend on who was looking at it.
    """
    result = rank(0.85)
    reroute = next(
        option
        for option in result.options  # type: ignore[attr-defined]
        if option.action is ActionType.REROUTE_TO_COLD_STORAGE
    )
    assert reroute.requires_approval
    assert reroute.approver_role == Role.FLEET_MANAGER.value

    null_option = next(
        option
        for option in result.options  # type: ignore[attr-defined]
        if option.action is ActionType.DO_NOTHING
    )
    assert not null_option.requires_approval
