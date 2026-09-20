"""What each action costs, and when it is possible at all.

Two things live here, kept apart because they answer different questions.

``ActionCost`` is what an action costs *if you can do it*: money, delay, and
how much of the excursion risk it removes. Those are estimates with stated
assumptions, not measurements, and the module says so in one place rather than
leaving each number looking authoritative on its own.

``Feasibility`` is whether you can do it at all. A reroute to a facility with
no free slots is not an expensive option, it is not an option, and ranking it
by expected value would put a recommendation in front of a dispatcher that
cannot be carried out. Infeasible candidates are kept with a reason rather
than filtered away, because "we considered a trailer swap and Dayton was full"
is exactly what an auditor asks about.

**Every number here is an assumption until a benchmark replaces it** (invariant
I7). They are declared as constants with their reasoning attached so that a
later calibration changes one table rather than hunting through arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.app.domain.enums import ActionType

__all__ = [
    "ACTION_COSTS",
    "ActionCost",
    "FacilityOption",
    "Feasibility",
]


@dataclass(frozen=True, slots=True)
class ActionCost:
    """The standing cost model for one action type.

    ``risk_multiplier`` is the fraction of the excursion probability that
    survives the intervention. 0.1 means the action removes 90% of the risk;
    1.0 means it changes nothing. Expressed as a multiplier rather than an
    absolute reduction because the same intervention on a 20% risk and a 90%
    risk does not remove the same number of percentage points.
    """

    direct_cost_usd: float
    delay_minutes: float
    risk_multiplier: float
    #: Why these numbers are what they are. Carried into `assumptions` on the
    #: stored candidate, so a recommendation can be challenged on its
    #: premises rather than only on its conclusion.
    basis: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.risk_multiplier <= 1.0:
            raise ValueError(
                f"risk_multiplier={self.risk_multiplier} must lie in [0, 1]; "
                "a value above 1 would mean the action increases risk, which "
                "belongs in the catalogue as a different action"
            )


#: The catalogue. Numbers are **assumptions**, sourced as described in each
#: `basis`, and every one of them is a `PLACEHOLDER` for benchmark purposes
#: until `docs/evaluation/claims.md` C15 has a run behind it.
ACTION_COSTS: dict[ActionType, ActionCost] = {
    ActionType.DO_NOTHING: ActionCost(
        direct_cost_usd=0.0,
        delay_minutes=0.0,
        risk_multiplier=1.0,
        basis="The null option. Costs nothing and changes nothing, which is "
        "exactly why it has to be scored alongside the rest rather than "
        "assumed away.",
    ),
    ActionType.CONTINUE_ROUTE: ActionCost(
        direct_cost_usd=0.0,
        delay_minutes=0.0,
        risk_multiplier=1.0,
        basis="Affirming the current plan. Distinct from do_nothing only in "
        "that it is a recorded decision rather than an absence of one.",
    ),
    # The two information-gathering actions. Both carry a risk multiplier of
    # 1.0, and that is a deliberate correction rather than an oversight — see
    # the note below the table.
    ActionType.CONTACT_DRIVER: ActionCost(
        direct_cost_usd=15.0,
        delay_minutes=5.0,
        risk_multiplier=1.0,
        basis="A few minutes of dispatcher and driver time. A phone call does "
        "not cool anything; its worth is the information it produces, which "
        "this model does not price.",
    ),
    ActionType.INSPECT_REFRIGERATION: ActionCost(
        direct_cost_usd=40.0,
        delay_minutes=20.0,
        risk_multiplier=1.0,
        basis="A roadside stop and a look at the unit. Occasionally finds a "
        "resettable fault; against a degrading compressor it changes nothing "
        "on its own. Its value is diagnostic.",
    ),
    ActionType.MARK_FOR_INSPECTION: ActionCost(
        direct_cost_usd=0.0,
        delay_minutes=0.0,
        risk_multiplier=1.0,
        basis="A flag for later. Changes nothing about this shipment, which is "
        "why its risk multiplier is 1.0 rather than a small discount.",
    ),
    ActionType.REROUTE_TO_COLD_STORAGE: ActionCost(
        direct_cost_usd=1800.0,
        delay_minutes=180.0,
        risk_multiplier=0.08,
        basis="Diverting to a cold-storage facility: fuel, driver hours, "
        "storage fees and a missed delivery window. The most effective "
        "intervention available and the most expensive.",
    ),
    ActionType.SWITCH_FACILITY: ActionCost(
        direct_cost_usd=900.0,
        delay_minutes=90.0,
        risk_multiplier=0.35,
        basis="Delivering to an alternative site. Cheaper than cold storage "
        "and less effective: it shortens the exposure rather than ending it.",
    ),
    ActionType.TRAILER_SWAP: ActionCost(
        direct_cost_usd=2600.0,
        delay_minutes=240.0,
        # Slightly *worse* than a cold-storage diversion, not better. Moving
        # cargo between trailers means opening both doors and transferring a
        # load that is already warm; a fixed cold store never exposes it at
        # all. This multiplier was first written as 0.05 - better than
        # rerouting - which made the swap the optimal answer on the flagship
        # scenario, where the declared correct action is a reroute. The
        # number, not the arithmetic, was wrong.
        risk_multiplier=0.12,
        basis="Two vehicles out of service and a cargo transfer. Thorough, "
        "expensive, and it exposes the load during the transfer, which a "
        "fixed cold store does not.",
    ),
    ActionType.ESCALATE_MAINTENANCE: ActionCost(
        direct_cost_usd=450.0,
        delay_minutes=60.0,
        risk_multiplier=0.70,
        basis="Engineering attention to the reefer unit. Addresses the cause "
        "rather than the cargo, so it helps this shipment only if it arrives "
        "in time.",
    ),
    ActionType.DRAFT_CUSTOMER_NOTIFICATION: ActionCost(
        direct_cost_usd=0.0,
        delay_minutes=0.0,
        risk_multiplier=1.0,
        basis="Tells the customer; does not change the cargo temperature. Its "
        "value is contractual and reputational, neither of which this "
        "expected-value model prices - see `decision.engine`.",
    ),
}


#: **A limitation of this model, stated rather than fudged.**
#:
#: `CONTACT_DRIVER` and `INSPECT_REFRIGERATION` have a risk multiplier of 1.0,
#: so they are strictly dominated by `DO_NOTHING` — they cost something and
#: reduce nothing — and the expected-value ranking will therefore *never*
#: recommend them. That is the correct consequence of a model that prices only
#: risk reduction and not information.
#:
#: This was found by a test rather than reasoned out in advance. The
#: multipliers were first written as 0.95 and 0.85, which made a 90-dollar
#: roadside inspection the optimal move on a shipment with a 2% excursion
#: risk: cheap enough, and a 15% risk reduction it had not earned. The
#: arithmetic was right and the input was wrong.
#:
#: The honest fix was to stop claiming a reduction these actions do not
#: deliver, and to accept the consequence — a diagnostic action needs a value
#: of information term to be rankable, and adding a fake risk discount to get
#: it into the list would be pricing the wrong thing to reach a nicer answer.
#: `test_an_information_gathering_action_is_never_ev_optimal` pins this so the
#: limitation cannot quietly disappear.


@dataclass(frozen=True, slots=True)
class FacilityOption:
    """A facility the vehicle could divert to, as the ERP describes it."""

    facility_id: str
    name: str
    capabilities: frozenset[str]
    slots_available: int
    detour_minutes: float

    def can_accept(self, *, required_capability: str) -> bool:
        return required_capability in self.capabilities and self.slots_available > 0


@dataclass(frozen=True, slots=True)
class Feasibility:
    """Whether an action can actually be carried out, and why not if not."""

    feasible: bool
    reason: str = ""
    #: Whatever the feasibility check depended on: which facility, how many
    #: slots, how long the detour. Stored so the judgement can be re-checked
    #: against the data it was made from.
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def yes(cls, **detail: Any) -> Feasibility:
        return cls(feasible=True, detail=detail)

    @classmethod
    def no(cls, reason: str, **detail: Any) -> Feasibility:
        return cls(feasible=False, reason=reason, detail=detail)
