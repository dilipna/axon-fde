"""Ranking costed options by expected value, and saying when the ranking is fragile.

Pure: no I/O, no LLM (invariant I9, enforced by an import-linter contract).
Facilities and risk arrive as arguments. That is what makes the ranking
reproducible and what keeps a language model out of the arithmetic — the model
may narrate this decision, never compute it.

**What expected value means here.** For each option:

    EV = -(direct cost) - (delay cost) - (surviving risk x cargo value)

All terms are negative, so EV is a loss and the best option is the one closest
to zero. Expressing it as a loss rather than a benefit avoids the question
"benefit relative to what?", which is how baselines get quietly chosen to
flatter a result.

**What it does not price.** Reputational damage, contractual penalties, the
regulatory consequence of a notified excursion, and the value of information
an action produces. `CONTACT_DRIVER` is therefore undervalued here: its real
worth is what it tells you, and this model cannot see that. Stated rather than
silently absorbed, because an expected-value number that looks complete is
more dangerous than one that admits its edges.

**The probability is not calibrated.** It comes from
`backend/app/risk/`, which returns a score monotone in risk, not a frequency.
Multiplying it by a cargo value produces a number with units of dollars and
the epistemic status of a guess. Every result therefore carries
`uses_calibrated_probability=False`, and claim C15 stays PLACEHOLDER until
B12's calibration exists. This is the single easiest place in the project to
ship a misleading figure.

**Flip sensitivity is part of the output, not an afterthought.** A ranking
that reverses when the probability moves two points is a ranking that should
be presented as "these are not distinguishable", and the caller cannot know
that unless the engine says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.decision.catalogue import ACTION_COSTS, Feasibility
from backend.app.domain.enums import ActionType

__all__ = [
    "DECISION_FLIP_TOLERANCE",
    "DELAY_COST_PER_MINUTE_USD",
    "ActionOption",
    "DecisionResult",
    "FlipPoint",
    "rank_options",
]

#: What an hour of delay costs, in dollars per minute. Covers driver time,
#: vehicle utilisation and the knock-on to the next load. An assumption, like
#: everything in the catalogue.
DELAY_COST_PER_MINUTE_USD = 2.5

#: Two options whose expected values differ by less than this fraction of the
#: cargo value are reported as not distinguishable. One percent of a
#: consignment is far inside the error on an uncalibrated probability, so
#: claiming an ordering inside that band would be false precision.
DECISION_FLIP_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class ActionOption:
    """One costed, feasibility-checked candidate."""

    action: ActionType
    feasibility: Feasibility

    direct_cost_usd: float
    delay_minutes: float
    delay_cost_usd: float
    residual_risk: float
    expected_loss_usd: float

    #: Negative: this is a loss. The least negative option wins.
    expected_value_usd: float
    requires_approval: bool
    approver_role: str | None
    assumptions: dict[str, Any]

    @property
    def feasible(self) -> bool:
        return self.feasibility.feasible

    def breakdown(self) -> dict[str, float]:
        """The arithmetic, so a recommendation can be checked term by term."""
        return {
            "direct_cost_usd": round(self.direct_cost_usd, 2),
            "delay_cost_usd": round(self.delay_cost_usd, 2),
            "expected_loss_usd": round(self.expected_loss_usd, 2),
            "expected_value_usd": round(self.expected_value_usd, 2),
            "residual_risk": round(self.residual_risk, 4),
        }


@dataclass(frozen=True, slots=True)
class FlipPoint:
    """The probability at which the top two options swap places.

    ``None`` when they never swap inside [0, 1], which means the ranking is
    robust across the whole range the probability could take. That is a
    stronger statement than "we are confident", and it is computable.
    """

    probability: float | None
    from_action: ActionType
    to_action: ActionType
    #: How far the current probability is from the flip. Small means the
    #: recommendation turns on a number nobody has calibrated.
    distance: float | None

    def describe(self) -> str:
        if self.probability is None:
            return (
                f"{self.from_action.value} beats {self.to_action.value} at every "
                "probability; the ranking does not depend on the risk estimate"
            )
        return (
            f"{self.from_action.value} gives way to {self.to_action.value} at "
            f"p={self.probability:.3f} (currently {self.distance:.3f} away)"
        )


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """The ranked options and how much to trust the ordering."""

    options: tuple[ActionOption, ...]
    probability: float
    cargo_value_usd: float
    flip: FlipPoint | None

    #: Always False in Phase 1. Present so that a consumer must actively
    #: decide to ignore it rather than never being told.
    uses_calibrated_probability: bool = False

    @property
    def feasible_options(self) -> tuple[ActionOption, ...]:
        return tuple(option for option in self.options if option.feasible)

    @property
    def recommended(self) -> ActionOption | None:
        """The best feasible option, or None when nothing is feasible.

        `do_nothing` is always feasible, so None here means the catalogue is
        empty rather than that the system has run out of ideas.
        """
        feasible = self.feasible_options
        return feasible[0] if feasible else None

    @property
    def is_close_call(self) -> bool:
        """Whether the top two options are distinguishable at all.

        A caller presenting a recommendation without checking this is
        presenting a preference as a conclusion.
        """
        feasible = self.feasible_options
        if len(feasible) < 2:
            return False
        margin = abs(feasible[0].expected_value_usd - feasible[1].expected_value_usd)
        return margin < DECISION_FLIP_TOLERANCE * self.cargo_value_usd


def _expected_value(
    action: ActionType, *, probability: float, cargo_value_usd: float
) -> tuple[float, float, float, float]:
    """Direct cost, delay cost, expected loss and total EV for one action."""
    cost = ACTION_COSTS[action]
    delay_cost = cost.delay_minutes * DELAY_COST_PER_MINUTE_USD
    residual_risk = probability * cost.risk_multiplier
    expected_loss = residual_risk * cargo_value_usd
    total = -(cost.direct_cost_usd + delay_cost + expected_loss)
    return cost.direct_cost_usd, delay_cost, expected_loss, total


def _flip_point(
    best: ActionOption, runner_up: ActionOption, *, probability: float, cargo_value_usd: float
) -> FlipPoint:
    """Where the top two options would swap, as the probability varies.

    Both expected values are linear in the probability, so the crossing is
    solved rather than searched:

        EV(a) = -(c_a + d_a) - p * m_a * V
        EV(b) = -(c_b + d_b) - p * m_b * V

    Setting them equal gives p = ((c_b + d_b) - (c_a + d_a)) / (V * (m_a - m_b)).
    Parallel lines - equal risk multipliers - never cross, which is reported
    as `None` rather than as a very large number.
    """
    best_cost = ACTION_COSTS[best.action]
    other_cost = ACTION_COSTS[runner_up.action]
    multiplier_gap = best_cost.risk_multiplier - other_cost.risk_multiplier

    if abs(multiplier_gap) < 1e-12 or cargo_value_usd <= 0:
        return FlipPoint(
            probability=None,
            from_action=best.action,
            to_action=runner_up.action,
            distance=None,
        )

    fixed_gap = (other_cost.direct_cost_usd + runner_up.delay_cost_usd) - (
        best_cost.direct_cost_usd + best.delay_cost_usd
    )
    crossing = fixed_gap / (cargo_value_usd * multiplier_gap)

    if not 0.0 <= crossing <= 1.0:
        # The lines cross outside the range a probability can take, so within
        # the real world the ordering never reverses.
        return FlipPoint(
            probability=None,
            from_action=best.action,
            to_action=runner_up.action,
            distance=None,
        )

    return FlipPoint(
        probability=crossing,
        from_action=best.action,
        to_action=runner_up.action,
        distance=abs(crossing - probability),
    )


def rank_options(
    *,
    probability: float,
    cargo_value_usd: float,
    feasibility: dict[ActionType, Feasibility],
    permitted: dict[ActionType, tuple[bool, str | None]] | None = None,
) -> DecisionResult:
    """Cost every candidate action and rank the feasible ones by expected value.

    ``do_nothing`` is always included and always feasible. A system that can
    only recommend action has a broken prior, and the expected-value
    comparison is meaningless without the null option in it — on a low-risk
    shipment `do_nothing` should and does win.

    Infeasible options are ranked last but **kept**, with their reason. "We
    considered a trailer swap and Dayton had no slots" is the answer to the
    question an auditor actually asks.

    ``permitted`` carries the policy engine's verdict per action. The decision
    engine does not call the policy engine itself — that would make this
    module impure and would let a ranking depend on who was looking at it.
    """
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"probability={probability} is outside [0, 1]")
    if cargo_value_usd < 0:
        raise ValueError(f"cargo_value_usd={cargo_value_usd} cannot be negative")

    approvals = permitted or {}
    options: list[ActionOption] = []

    for action in ACTION_COSTS:
        verdict = feasibility.get(action, Feasibility.yes())
        if action is ActionType.DO_NOTHING:
            # Always on the table, whatever the feasibility map says.
            verdict = Feasibility.yes(note="the null option is always available")

        direct, delay_cost, expected_loss, total = _expected_value(
            action, probability=probability, cargo_value_usd=cargo_value_usd
        )
        cost = ACTION_COSTS[action]
        needs_approval, approver = approvals.get(action, (False, None))

        options.append(
            ActionOption(
                action=action,
                feasibility=verdict,
                direct_cost_usd=direct,
                delay_minutes=cost.delay_minutes,
                delay_cost_usd=delay_cost,
                residual_risk=probability * cost.risk_multiplier,
                expected_loss_usd=expected_loss,
                expected_value_usd=total,
                requires_approval=needs_approval,
                approver_role=approver,
                assumptions={
                    "basis": cost.basis,
                    "risk_multiplier": cost.risk_multiplier,
                    "delay_cost_per_minute_usd": DELAY_COST_PER_MINUTE_USD,
                    "probability_is_calibrated": False,
                    **verdict.detail,
                },
            )
        )

    # Feasible first, then by expected value descending (least loss wins).
    #
    # Ties favour `do_nothing`, then fall back to the action name. Both parts
    # matter. Preferring inaction when the arithmetic cannot separate two
    # options is a deliberate bias: an intervention that is merely *not worse*
    # in expectation still costs real money and disrupts a real delivery, and
    # a system whose ties break toward doing something acquires exactly the
    # bias the false-alarm control exists to catch. The name is the final
    # tiebreak only so the ordering is deterministic - an unstable sort would
    # make a stored recommendation unreproducible.
    options.sort(
        key=lambda option: (
            not option.feasible,
            -option.expected_value_usd,
            option.action is not ActionType.DO_NOTHING,
            option.action.value,
        )
    )

    feasible = [option for option in options if option.feasible]
    flip = (
        _flip_point(
            feasible[0], feasible[1], probability=probability, cargo_value_usd=cargo_value_usd
        )
        if len(feasible) >= 2
        else None
    )

    return DecisionResult(
        options=tuple(options),
        probability=probability,
        cargo_value_usd=cargo_value_usd,
        flip=flip,
    )
