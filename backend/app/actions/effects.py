"""What an action is supposed to achieve, stated before it is taken.

Every executor declares its expected effect at execution time, and outcome
verification checks that declaration against what the sensors later reported.
Declaring it *before* acting is the whole point: an expected effect written
afterwards is a description of what happened, and grading a system against a
target chosen once the result is known measures nothing.

This lives in ``actions`` rather than ``verification`` because the executor is
what knows the claim. A reroute claims the cargo comes back into spec; a
maintenance escalation claims only that the rise stops. Those are different
promises and a system that verified both against "did it come back in spec?"
would mark a successful escalation as a failure.

Pure, and deliberately small: three effect kinds, because a vocabulary that
grows per action becomes a second action catalogue that drifts from the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["MIN_SLOPE_WINDOW_MINUTES", "EffectKind", "ExpectedEffect"]

#: The shortest window over which "has the rise stopped?" is answerable at all.
#:
#: **This number is measured, not chosen.** Fitting a rolling least-squares
#: slope to the two recordings gives, in degrees Celsius per minute:
#:
#: | window | healthy control, worst | degrading truck, best | separable |
#: |--------|-----------------------|----------------------|-----------|
#: | 30 min | 0.0214                | 0.0105               | no        |
#: | 45 min | 0.0168                | 0.0125               | no        |
#: | 60 min | 0.0126                | 0.0136               | barely    |
#: | 90 min | 0.0080                | 0.0153               | yes       |
#:
#: Below an hour the distributions overlap outright: a perfectly healthy run
#: produces slopes steeper than a failing compressor's shallowest, so *no*
#: threshold separates them and any verdict is a coin toss dressed as a
#: measurement. At 60 minutes they barely clear each other, with the gap
#: narrower than the spread within either. Ninety leaves close to a factor of
#: two.
#:
#: The first draft of the spec table set this window from operational
#: intuition - a phone call resolves in twenty minutes, so twenty minutes - and
#: that is the error the table above caught. How long an intervention takes to
#: work and how long the data needs to show that it worked are different
#: durations, and only the second one belongs here.
MIN_SLOPE_WINDOW_MINUTES = 90


class EffectKind(StrEnum):
    """The closed set of claims an action may make about the future."""

    #: The cargo is back inside its contractual envelope. Claimed by anything
    #: that changes where the cargo goes or what cools it - and by
    #: `do_nothing`, which is a prediction that it stays in spec rather than
    #: an absence of one. Verifying the null option is what stops "we decided
    #: it was fine" from being unfalsifiable.
    TEMPERATURE_WITHIN_ENVELOPE = "temperature_within_envelope"

    #: The rise has stopped, whether or not the cargo is back in spec.
    #: Claimed by interventions that address the cause without moving the
    #: cargo - an inspection, a maintenance escalation, a call to the driver.
    #: Grading these against a full recovery would fail an escalation that
    #: worked exactly as intended.
    TEMPERATURE_STOPS_RISING = "temperature_stops_rising"

    #: Nothing a sensor can see. Flagging a vehicle for inspection or drafting
    #: a customer notification changes a record, not a temperature. Verified
    #: as *not applicable* rather than as a pass, because counting an
    #: unobservable action as a success would inflate every outcome metric
    #: with actions nobody could check.
    NONE_OBSERVABLE = "none_observable"


@dataclass(frozen=True, slots=True)
class ExpectedEffect:
    """A claim about the world, and how long it has to come true."""

    kind: EffectKind
    #: How long after execution the claim is checked. Per action, because a
    #: trailer swap takes longer to show up in the data than a door being
    #: closed, and one global window would fail the slow actions and pass the
    #: fast ones before they had done anything.
    within_minutes: int
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind is EffectKind.NONE_OBSERVABLE:
            if self.within_minutes != 0:
                raise ValueError(
                    "an unobservable effect cannot have a verification window; "
                    "a window implies something will be measured at the end of it"
                )
        elif self.within_minutes <= 0:
            raise ValueError(
                f"within_minutes={self.within_minutes} leaves no time for the "
                "effect to occur, so the verification would always fail"
            )
        elif (
            self.kind is EffectKind.TEMPERATURE_STOPS_RISING
            and self.within_minutes < MIN_SLOPE_WINDOW_MINUTES
        ):
            # Refused at construction rather than handled at verification
            # time, so an action cannot declare a claim the data could never
            # settle. The alternative - accepting the window and returning
            # `inconclusive` for ever - hides an unanswerable question inside
            # a result that looks like a measurement.
            raise ValueError(
                f"a {self.kind.value} claim needs at least "
                f"{MIN_SLOPE_WINDOW_MINUTES} minutes to be answerable, not "
                f"{self.within_minutes}; below that a healthy run and a failing "
                "one produce overlapping slopes"
            )

    def as_payload(self) -> dict[str, Any]:
        """The JSONB form stored on the verification row."""
        return {
            "kind": self.kind.value,
            "within_minutes": self.within_minutes,
            **self.detail,
        }
