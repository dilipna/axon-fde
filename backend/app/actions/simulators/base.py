"""The shape every simulated executor has.

Phase 1 executes nothing real. These simulators stand where an integration
with a TMS, a maintenance system or a telephony provider will go, and they
exist now rather than later because the governance machinery around them -
approval binding, idempotency, verification, audit - is what is being built
and tested, and that machinery cannot be exercised against nothing.

**Simulated does not mean arbitrary.** Two properties are load-bearing and
both are tested:

1. *Determinism.* A simulator's result is a pure function of its request. No
   clock, no ``random``, no counter. A reference number drawn from
   ``random`` would differ on every run, and the golden-digest style used
   elsewhere in this repository would be impossible - worse, an idempotency
   test would still pass, because it returns the *stored* row, so the
   non-determinism would hide exactly where it mattered least to find it.
2. *A declared expected effect.* Each simulator states what success looks
   like before acting, and outcome verification grades against that
   declaration. See ``backend/app/actions/effects.py``.

The protocol takes a request dict rather than a typed per-action argument.
That is a deliberate concession: the request is stored as JSONB on
``action_execution`` and replayed from there, so a typed layer would have to
round-trip through the same dict anyway, and the two representations would
drift.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.app.actions.effects import ExpectedEffect
from backend.app.domain.enums import ActionType

__all__ = [
    "ActionSimulator",
    "SimulatedOutcome",
    "reference_number",
]


def reference_number(prefix: str, idempotency_key: str) -> str:
    """A stable, human-quotable reference derived from the request.

    Derived rather than generated, so that re-running a scenario produces the
    same references and a stored result can be compared byte for byte. The
    digest is truncated to ten hex characters: long enough that a collision
    inside one incident is not a practical concern, short enough that a
    dispatcher can read it down a phone line.
    """
    digest = hashlib.blake2b(idempotency_key.encode("utf-8"), digest_size=5).hexdigest()
    return f"{prefix}-{digest.upper()}"


@dataclass(frozen=True, slots=True)
class SimulatedOutcome:
    """What an executor did, and what it claims will follow."""

    action: ActionType
    #: Stored as ``action_execution.result``. Everything a human would need to
    #: reconstruct what was done without access to the system that did it.
    result: dict[str, Any]
    expected_effect: ExpectedEffect
    #: Free text for the audit payload and the dispatcher's screen.
    summary: str = ""
    #: What the caller asked for, echoed back and stored as
    #: ``action_execution.request``. Held here so the stored row records the
    #: request as the executor understood it, which is not always the request
    #: as the caller wrote it.
    request: dict[str, Any] = field(default_factory=dict)


class ActionSimulator(Protocol):
    """One executor. Pure: same request in, same outcome out."""

    @property
    def action(self) -> ActionType: ...

    def execute(self, request: dict[str, Any], *, idempotency_key: str) -> SimulatedOutcome: ...
