"""Binding an approval to the world it was granted against.

An approval given at 14:02 against a 78% excursion probability is not an
approval of the same action at 14:20 after three new readings arrived. The
bound hash is what makes that statement enforceable rather than aspirational:
it is computed from everything the approver saw, stored on the approval, and
recomputed at execution time. A mismatch means the world moved and the
decision must be re-taken.

**What the hash covers, and why each part is in it.**

- *Evidence content hashes.* The observations the recommendation rested on. A
  new reading, a retracted one, a corrected document - any of these change the
  set, and any of them could change the answer.
- *The risk probability and its baseline.* This is the part most easily left
  out, and leaving it out is the failure the whole mechanism exists to
  prevent: a 40% decision executed against an 85% world. The baseline is in
  too, because a run where the model and the baseline agree is a different
  thing to approve than one where they diverge, even at the same headline
  number. ``degraded`` is in for the same reason - approving a number the
  model produced is not the same as approving a rule's guess made because the
  model was unavailable.
- *The selected action and its target.* Approving a reroute to Dayton is not
  approving a reroute to Columbus.
- *The incident.* So an approval cannot be replayed against a different one.

**The binding is exact, not tolerant.** There is no epsilon on the
probability. That is deliberate: a tolerance needs a defensible definition of
"small", and there is not one here - whether a 0.02 move matters depends on
the flip point, which is a separate computation with its own inputs. Rounding
would also be a quantisation rather than a band, so two values a nanometre
apart could still straddle a boundary and hash differently: the appearance of
tolerance without the substance. Fail closed and ask a human again.

``BINDING_VERSION`` is in the hash so that widening what is covered cannot
silently leave old approvals matching. Change the covered set and every
outstanding approval becomes stale, which is the correct outcome: they were
bound to less than the new rule requires.

The module is pure - no I/O, no clock, no database. That is what lets the same
function run in the service, in a test and in a benchmark grader without three
implementations drifting apart.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from backend.app.domain.enums import ActionType

__all__ = [
    "BINDING_VERSION",
    "ApprovalContext",
    "compute_bound_hash",
]

#: Bumped whenever the covered set changes. Part of the hash, so a bump
#: invalidates every outstanding approval rather than leaving some bound to an
#: older, narrower definition of "the world".
BINDING_VERSION = 1


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    """Everything an approver was shown, in hashable form.

    Constructed from the same objects the dispatcher's screen was rendered
    from. If a field is displayed to a human and could change the decision, it
    belongs here; if it is in here and is not displayed, the approval is bound
    to something nobody saw, which is its own kind of wrong.
    """

    incident_id: UUID
    action: ActionType
    #: Which facility, which trailer. ``None`` for actions with no target,
    #: such as `do_nothing`.
    target_ref: str | None

    #: Content hashes of the evidence behind the recommendation. Order is not
    #: meaningful - they are sorted before hashing, so the same evidence read
    #: in a different order binds identically.
    evidence_hashes: Sequence[str]

    risk_probability: float
    baseline_probability: float
    baseline_name: str
    model_version: str
    horizon_minutes: int
    #: True when the primary estimator was unavailable. Bound because
    #: approving a model's number and approving a fallback rule's number are
    #: different decisions even when the numbers coincide.
    degraded: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("risk_probability", self.risk_probability),
            ("baseline_probability", self.baseline_probability),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name}={value} is outside [0, 1]")
        if not self.evidence_hashes:
            # An approval bound to no evidence is bound to nothing: it would
            # survive any change to the observations and the staleness check
            # would be decorative. A recommendation with no evidence behind it
            # is a bug upstream, and this is where it becomes visible.
            raise ValueError(
                "an approval context must cite at least one piece of evidence; "
                "binding to an empty set would make the staleness check inert"
            )

    def canonical(self) -> dict[str, Any]:
        """The exact structure that gets hashed.

        Exposed rather than private so that a mismatch can be *diagnosed*.
        "The approval is stale" is unactionable on its own; diffing two of
        these says which field moved, which is what an operator needs at
        14:20 on a degrading truck.
        """
        return {
            "binding_version": BINDING_VERSION,
            "incident_id": str(self.incident_id),
            "action": self.action.value,
            "target_ref": self.target_ref,
            "evidence_hashes": sorted(self.evidence_hashes),
            "risk": {
                "probability": float(self.risk_probability),
                "baseline_probability": float(self.baseline_probability),
                "baseline_name": self.baseline_name,
                "model_version": self.model_version,
                "horizon_minutes": int(self.horizon_minutes),
                "degraded": bool(self.degraded),
            },
        }

    def bound_hash(self) -> str:
        return compute_bound_hash(self)

    def differences_from(self, other: ApprovalContext) -> tuple[str, ...]:
        """Which covered fields differ, for the operator-facing message.

        Compares the canonical forms, so it reports exactly the fields the
        hash is computed over - a diff that disagreed with the hash would be
        worse than no diff at all.
        """
        mine, theirs = self.canonical(), other.canonical()
        changed: list[str] = []
        for key in sorted(set(mine) | set(theirs)):
            if mine.get(key) == theirs.get(key):
                continue
            if key != "risk":
                changed.append(key)
                continue
            # Named per field, because "risk changed" does not distinguish a
            # probability that moved from a model that fell back to a rule.
            risk_mine: dict[str, Any] = mine.get(key) or {}
            risk_theirs: dict[str, Any] = theirs.get(key) or {}
            changed.extend(
                f"risk.{field}"
                for field in sorted(set(risk_mine) | set(risk_theirs))
                if risk_mine.get(field) != risk_theirs.get(field)
            )
        return tuple(changed)


def compute_bound_hash(context: ApprovalContext) -> str:
    """SHA-256 over the canonical form of what the approver saw.

    ``sort_keys`` and the tight separators make the serialisation
    byte-identical for equal content, so the hash does not depend on dict
    insertion order or on how a caller happened to build the object. Floats
    serialise through Python's shortest round-trippable repr, which is a
    one-to-one mapping for float64 - equal values always produce equal bytes.
    """
    payload = json.dumps(context.canonical(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
