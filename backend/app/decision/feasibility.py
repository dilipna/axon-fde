"""Whether each action can actually be carried out, given facility data.

Separate from the cost model because these are different kinds of fact. A
reroute costs roughly the same whenever you do it; whether there is a free
slot at a pharma-certified facility within range changes hour by hour.

Pure, like the rest of `decision`. Facilities arrive as arguments — the ERP
read happens in the adapter layer, and doing it here would make a ranking
depend on a database round trip that could fail mid-decision.

**Infeasible is not expensive.** Ranking an impossible option by expected
value would put a recommendation in front of a dispatcher that cannot be
carried out, which is worse than offering one fewer choice. So feasibility is
a gate, and the reason is carried rather than discarded.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.app.decision.catalogue import FacilityOption, Feasibility
from backend.app.domain.enums import ActionType

__all__ = [
    "MAX_DETOUR_MINUTES",
    "PHARMA_CAPABILITY",
    "TRAILER_SWAP_CAPABILITY",
    "assess_feasibility",
]

#: Cold storage that may accept pharmaceutical cargo. A facility that can keep
#: something cold but is not certified for pharma cannot take this load, and
#: treating the two as interchangeable is how a compliance breach gets
#: recommended by a system that was trying to help.
PHARMA_CAPABILITY = "pharma_certified"
TRAILER_SWAP_CAPABILITY = "trailer_swap"

#: A detour longer than this arrives after the cargo is already ruined, so the
#: option is not a rescue. Set against the 60-minute risk horizon with room for
#: the intervention itself to take effect.
MAX_DETOUR_MINUTES = 120.0


def _best_facility(
    facilities: Sequence[FacilityOption], *, capability: str
) -> FacilityOption | None:
    """The nearest facility with the capability and a free slot."""
    usable = [
        facility
        for facility in facilities
        if facility.can_accept(required_capability=capability)
        and facility.detour_minutes <= MAX_DETOUR_MINUTES
    ]
    return min(usable, key=lambda facility: facility.detour_minutes) if usable else None


def assess_feasibility(
    *,
    facilities: Sequence[FacilityOption] = (),
    requires_pharma_certification: bool = True,
    minutes_to_destination: float | None = None,
    driver_contactable: bool = True,
) -> dict[ActionType, Feasibility]:
    """Decide which actions are possible, with a reason for each that is not.

    Returns an entry for every action, including the possible ones, so a
    caller never has to distinguish "feasible" from "not mentioned".
    """
    capability = PHARMA_CAPABILITY if requires_pharma_certification else "cold_storage"

    cold_storage = _best_facility(facilities, capability=capability)
    swap_site = _best_facility(facilities, capability=TRAILER_SWAP_CAPABILITY)

    verdicts: dict[ActionType, Feasibility] = {
        ActionType.DO_NOTHING: Feasibility.yes(),
        ActionType.CONTINUE_ROUTE: Feasibility.yes(),
        ActionType.MARK_FOR_INSPECTION: Feasibility.yes(),
        ActionType.ESCALATE_MAINTENANCE: Feasibility.yes(),
        ActionType.DRAFT_CUSTOMER_NOTIFICATION: Feasibility.yes(),
    }

    verdicts[ActionType.CONTACT_DRIVER] = (
        Feasibility.yes() if driver_contactable else Feasibility.no("the driver is not reachable")
    )
    # Inspecting the unit means asking the driver to do it.
    verdicts[ActionType.INSPECT_REFRIGERATION] = (
        Feasibility.yes()
        if driver_contactable
        else Feasibility.no("an inspection needs the driver, who is not reachable")
    )

    if cold_storage is None:
        candidates = ", ".join(
            f"{facility.facility_id}({facility.slots_available} slots)" for facility in facilities
        )
        verdicts[ActionType.REROUTE_TO_COLD_STORAGE] = Feasibility.no(
            f"no {capability} facility with a free slot within {MAX_DETOUR_MINUTES:.0f} minutes",
            required_capability=capability,
            considered=candidates or "none",
        )
        verdicts[ActionType.SWITCH_FACILITY] = Feasibility.no(
            f"no {capability} facility with a free slot within {MAX_DETOUR_MINUTES:.0f} minutes",
            required_capability=capability,
        )
    else:
        verdicts[ActionType.REROUTE_TO_COLD_STORAGE] = Feasibility.yes(
            facility_id=cold_storage.facility_id,
            facility_name=cold_storage.name,
            detour_minutes=cold_storage.detour_minutes,
            slots_available=cold_storage.slots_available,
        )
        verdicts[ActionType.SWITCH_FACILITY] = Feasibility.yes(
            facility_id=cold_storage.facility_id,
            detour_minutes=cold_storage.detour_minutes,
        )

    if swap_site is None:
        verdicts[ActionType.TRAILER_SWAP] = Feasibility.no(
            f"no {TRAILER_SWAP_CAPABILITY} facility with a free slot within "
            f"{MAX_DETOUR_MINUTES:.0f} minutes",
            required_capability=TRAILER_SWAP_CAPABILITY,
        )
    else:
        verdicts[ActionType.TRAILER_SWAP] = Feasibility.yes(
            facility_id=swap_site.facility_id,
            detour_minutes=swap_site.detour_minutes,
            slots_available=swap_site.slots_available,
        )

    # A shipment about to arrive cannot usefully be rerouted: the detour costs
    # more time than remains, so the cargo reaches its destination either way.
    if minutes_to_destination is not None and minutes_to_destination < 30.0:
        for action in (ActionType.REROUTE_TO_COLD_STORAGE, ActionType.TRAILER_SWAP):
            verdicts[action] = Feasibility.no(
                f"only {minutes_to_destination:.0f} minutes remain; the "
                "intervention would arrive after the cargo does",
                minutes_to_destination=minutes_to_destination,
            )

    return verdicts
