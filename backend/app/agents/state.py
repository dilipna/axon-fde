"""The workflow's state, and what may be written to it.

Typed rather than a bare dict, because this object is checkpointed to Postgres
after every node and resumed from there. A dict with ad-hoc keys survives the
first week and then becomes the reason nobody can say what a resumed run will
do.

**Two rules about what lives here.**

*No confidence a model produced.* ``hypotheses`` holds ``ScoredHypothesis``
objects, every one of which was scored by the deterministic scorer. The
model's output reaches this state only as ``ProposedLink`` - which has no
number in it - and as narrative text that the grounding check has passed.

*No evidence a model produced.* Evidence enters at ``ingest`` from the
adapters and is never appended to by a later node. A node that could add an
observation would make the model a source, which is invariant I1 and the
thing the whole grounding story rests on.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict
from uuid import UUID

from backend.app.agents.budget import BudgetState
from backend.app.agents.grounding import GroundingReport
from backend.app.agents.scoring import ProposedLink, ScoredHypothesis
from backend.app.decision.engine import DecisionResult
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.risk.service import RiskEstimate

__all__ = ["IncidentState", "TraceEntry", "append_trace"]


class TraceEntry(TypedDict):
    """One line of the run's own history."""

    node: str
    detail: str


def append_trace(existing: list[TraceEntry], incoming: list[TraceEntry]) -> list[TraceEntry]:
    """Reducer for the trace: nodes append, nothing overwrites.

    Every other field in the state is last-write-wins, which is right for a
    value that one node owns. The trace is the exception - it is the record of
    what ran, and a node that replaced it would erase the steps before it.
    """
    return [*existing, *incoming]


class IncidentState(TypedDict, total=False):
    """Everything the workflow knows, at any point in the run."""

    # -- identity ---------------------------------------------------------
    incident_id: UUID
    shipment_id: str
    vehicle_id: str
    cargo_value_usd: float

    # -- what is known ----------------------------------------------------
    #: Written once, at `ingest`. No later node appends to it: a model that
    #: could add an observation would be a source (I1).
    evidence: list[Evidence]
    envelope: TemperatureEnvelope | None
    conflicts: list[str]
    risk: RiskEstimate | None

    # -- what the model proposed, and what the scorer made of it ----------
    proposed_links: list[ProposedLink]
    hypotheses: list[ScoredHypothesis]

    # -- what was decided -------------------------------------------------
    decision: DecisionResult | None
    narrative: str
    cited_evidence_ids: list[str]
    grounding: GroundingReport | None

    # -- control ----------------------------------------------------------
    budget: BudgetState
    #: Set when a budget ran out or a node could not proceed. Its presence is
    #: what routes the graph to `escalate`, so it is never cleared once set -
    #: a run that escalated and then carried on would be the worst of both.
    escalation_reason: str
    trace: Annotated[list[TraceEntry], append_trace]
    #: Free-form, for anything a node wants an auditor to see later.
    notes: dict[str, Any]
