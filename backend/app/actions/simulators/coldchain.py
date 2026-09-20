"""The ten cold-chain executors.

One parameterised class rather than ten near-identical ones. The interesting
content is the table below, and putting it in one place means every claim the
system makes about what an action achieves is readable side by side - which is
what makes it obvious that `escalate_maintenance` promises only that the rise
stops, while `reroute_to_cold_storage` promises a recovery.

**A missing request field is a refusal, not a default.** A reroute with no
destination cannot be simulated into existence; inventing one would be
fabricating an observation about where a truck went, and the record would
then contain a facility nobody chose. This is invariant I6 applied to
actions: absence is represented as absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.actions.effects import MIN_SLOPE_WINDOW_MINUTES, EffectKind, ExpectedEffect
from backend.app.actions.simulators.base import SimulatedOutcome, reference_number
from backend.app.domain.enums import ActionType

__all__ = ["ACTION_SPECS", "SimulatedExecutor", "build_simulators"]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Everything that distinguishes one simulated executor from another."""

    #: Prefix on the generated reference number, so a reference is readable
    #: without a lookup: `RRT-...` is a reroute, `WO-...` a work order.
    prefix: str
    effect: EffectKind
    #: Minutes the claim has to come true. Zero only for unobservable effects.
    window_minutes: int
    #: Fields the request must carry. Missing one is a refusal.
    required_fields: tuple[str, ...]
    summary: str


#: Every member of ``ActionType`` appears exactly once, and a test asserts it.
#: An action missing from here cannot be executed at all, which is safe, but
#: silently unexecutable, which is not.
ACTION_SPECS: dict[ActionType, ActionSpec] = {
    ActionType.DO_NOTHING: ActionSpec(
        prefix="NOP",
        # Not NONE_OBSERVABLE. Choosing to do nothing is a prediction that the
        # cargo stays in spec, and it is checked like any other prediction.
        # Exempting it would make inaction the one decision nobody grades.
        effect=EffectKind.TEMPERATURE_WITHIN_ENVELOPE,
        window_minutes=60,
        required_fields=(),
        summary="No intervention; the shipment is expected to remain in specification.",
    ),
    ActionType.CONTINUE_ROUTE: ActionSpec(
        prefix="CNT",
        effect=EffectKind.TEMPERATURE_WITHIN_ENVELOPE,
        window_minutes=60,
        required_fields=(),
        summary="Route confirmed unchanged.",
    ),
    ActionType.CONTACT_DRIVER: ActionSpec(
        prefix="CALL",
        effect=EffectKind.TEMPERATURE_STOPS_RISING,
        # Ninety minutes, not the twenty a phone call takes. The window is how
        # long the *data* needs to answer the question, and below 90 a healthy
        # run and a failing one produce overlapping slopes - see
        # MIN_SLOPE_WINDOW_MINUTES for the measurement.
        window_minutes=MIN_SLOPE_WINDOW_MINUTES,
        required_fields=("vehicle_id",),
        summary="Driver contacted and asked to check the load and the reefer unit.",
    ),
    ActionType.INSPECT_REFRIGERATION: ActionSpec(
        prefix="INSP",
        effect=EffectKind.TEMPERATURE_STOPS_RISING,
        window_minutes=MIN_SLOPE_WINDOW_MINUTES,
        required_fields=("vehicle_id",),
        summary="Refrigeration inspection requested at the next safe stop.",
    ),
    ActionType.REROUTE_TO_COLD_STORAGE: ActionSpec(
        prefix="RRT",
        effect=EffectKind.TEMPERATURE_WITHIN_ENVELOPE,
        # Long enough to cover the detour and the transfer into cold storage.
        # A shorter window would grade a reroute as failed while the truck was
        # still driving to the facility.
        window_minutes=90,
        required_fields=("vehicle_id", "facility_id"),
        summary="Vehicle rerouted to cold storage.",
    ),
    ActionType.SWITCH_FACILITY: ActionSpec(
        prefix="SWF",
        effect=EffectKind.TEMPERATURE_WITHIN_ENVELOPE,
        window_minutes=120,
        required_fields=("shipment_id", "facility_id"),
        summary="Delivery reassigned to an alternate facility.",
    ),
    ActionType.TRAILER_SWAP: ActionSpec(
        prefix="SWP",
        effect=EffectKind.TEMPERATURE_WITHIN_ENVELOPE,
        window_minutes=120,
        required_fields=("vehicle_id", "replacement_vehicle_id", "swap_location"),
        summary="Trailer swap scheduled; cargo transfers to a serviceable unit.",
    ),
    ActionType.ESCALATE_MAINTENANCE: ActionSpec(
        prefix="WO",
        # Only that the rise stops. A work order does not move cargo, and
        # grading it against a full recovery would fail an escalation that did
        # exactly what was asked of it.
        effect=EffectKind.TEMPERATURE_STOPS_RISING,
        window_minutes=MIN_SLOPE_WINDOW_MINUTES,
        required_fields=("vehicle_id",),
        summary="Priority maintenance work order raised against the vehicle.",
    ),
    ActionType.MARK_FOR_INSPECTION: ActionSpec(
        prefix="FLAG",
        effect=EffectKind.NONE_OBSERVABLE,
        window_minutes=0,
        required_fields=("vehicle_id",),
        summary="Vehicle flagged for inspection on arrival.",
    ),
    ActionType.DRAFT_CUSTOMER_NOTIFICATION: ActionSpec(
        prefix="DRFT",
        effect=EffectKind.NONE_OBSERVABLE,
        window_minutes=0,
        required_fields=("shipment_id",),
        # Drafted, not sent. The distinction is the entire reason this action
        # needs a compliance officer's signature: a draft is reviewable, and a
        # sent message to a pharmaceutical customer is a regulatory statement
        # that cannot be recalled.
        summary="Customer notification drafted for review. Not sent.",
    ),
}


@dataclass(frozen=True, slots=True)
class SimulatedExecutor:
    """One action's simulated executor, driven by its spec."""

    action: ActionType
    spec: ActionSpec

    def execute(self, request: dict[str, Any], *, idempotency_key: str) -> SimulatedOutcome:
        """Carry out the action, deterministically.

        Raises:
            ValueError: The request is missing a field the action cannot be
                carried out without.
        """
        missing = [name for name in self.spec.required_fields if not request.get(name)]
        if missing:
            raise ValueError(
                f"{self.action.value} cannot be executed without {', '.join(sorted(missing))}; "
                "defaulting them would put a destination nobody chose into the record"
            )

        accepted = {name: request[name] for name in self.spec.required_fields}
        reference = reference_number(self.spec.prefix, idempotency_key)

        return SimulatedOutcome(
            action=self.action,
            result={
                "reference": reference,
                "simulated": True,
                "status": "accepted",
                **accepted,
            },
            expected_effect=ExpectedEffect(
                kind=self.spec.effect,
                within_minutes=self.spec.window_minutes,
            ),
            summary=f"{self.spec.summary} Reference {reference}.",
            request=dict(request),
        )


def build_simulators() -> dict[ActionType, SimulatedExecutor]:
    """The registry, built from the spec table."""
    return {
        action: SimulatedExecutor(action=action, spec=spec) for action, spec in ACTION_SPECS.items()
    }
