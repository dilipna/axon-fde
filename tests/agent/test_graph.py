"""The workflow, run end to end.

**No cassettes exist yet**, because nothing has been recorded against the real
API. So the two model-facing nodes are supplied here as scripted functions
that satisfy the same signature the Anthropic-backed ones will. That is not a
substitute for cassettes and is not pretending to be: it tests the *graph* -
the routing, the budget gates, the deterministic checks, and what happens when
model output is rejected - and says nothing about what a model would actually
produce. Recording real cassettes needs an API key and a deliberate session.

What these tests do protect is the part that must not depend on the model at
all: that a proposed link cannot set a confidence, that a fabricated citation
stops the run before an approval is requested, and that a spent budget
escalates rather than spinning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from backend.app.agents.budget import Budget, BudgetState
from backend.app.agents.graph import (
    ESCALATE,
    NODE_NAMES,
    IncidentWorkflow,
    build_graph,
    initial_state,
)
from backend.app.agents.scoring import ProposedLink
from backend.app.agents.state import IncidentState
from backend.app.decision.catalogue import FacilityOption
from backend.app.domain.enums import EvidenceSource, Modality, RootCause
from backend.app.domain.evidence import EntityRef, Evidence, Provenance

pytestmark = pytest.mark.agent

START = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)

GARY = FacilityOption(
    facility_id="CS-11",
    name="Gary Cold Storage",
    capabilities=frozenset({"cold_storage", "pharma_certified", "trailer_swap"}),
    slots_available=3,
    detour_minutes=45.0,
)


def observation(
    observation_type: str,
    value: float | str | list[str],
    *,
    at: datetime,
) -> Evidence:
    return Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id="AX-042"),
        source=EvidenceSource.TELEMETRY,
        modality=Modality.TIMESERIES,
        observation_type=observation_type,
        value=value,
        observed_at=at,
        ingested_at=at + timedelta(seconds=5),
        provenance=Provenance(producer="test_graph", producer_version="1.0.0", note="fixture"),
    )


def rising_bundle() -> list[Evidence]:
    """A degrading trailer: 40 minutes of climb against an 8.0 C ceiling."""
    evidence: list[Evidence] = [
        observation("permitted_temp_max_c", 8.0, at=START),
        observation("permitted_temp_min_c", 2.0, at=START),
    ]
    evidence.extend(
        observation("cargo_temp_c", 5.5 + 0.06 * minute, at=START + timedelta(minutes=minute))
        for minute in range(41)
    )
    return evidence


def _temp_ids(state: IncidentState, count: int) -> list[str]:
    return [str(item.id) for item in state["evidence"] if item.observation_type == "cargo_temp_c"][
        -count:
    ]


def scripted_links(*, cite: list[str] | None = None, tokens: int = 1200):
    async def propose(state: IncidentState) -> dict[str, Any]:
        ids = cite if cite is not None else _temp_ids(state, 3)
        return {
            "proposed_links": [
                ProposedLink(
                    root_cause=RootCause.COMPRESSOR_DEGRADATION,
                    evidence_ids=tuple(ids),
                    rationale="Temperature is climbing steadily against the ceiling.",
                )
            ],
            "_tokens": tokens,
        }

    return propose


def scripted_narrative(text: str | None = None, *, cite: list[str] | None = None):
    async def narrate(state: IncidentState) -> dict[str, Any]:
        risk = state.get("risk")
        probability = risk.probability if risk else 0.0
        ids = cite if cite is not None else _temp_ids(state, 1)
        return {
            "narrative": text
            or f"Breach probability is {probability:.2f} against a ceiling of 8.0 C.",
            "cited_evidence_ids": ids,
            "_tokens": 900,
        }

    return narrate


def workflow(**overrides: Any) -> IncidentWorkflow:
    settings: dict[str, Any] = {
        "propose_links": scripted_links(),
        "narrate": scripted_narrative(),
        "facilities": [GARY],
        "now": lambda: START + timedelta(minutes=41),
    }
    settings.update(overrides)
    return IncidentWorkflow(**settings)


async def run(
    flow: IncidentWorkflow,
    *,
    budget: Budget | None = None,
    started_at: datetime | None = None,
) -> IncidentState:
    """Run the graph once.

    ``started_at`` defaults to the workflow's own clock, not to the evidence's
    first timestamp. **Those are two different clocks**, and conflating them is
    how the first version of this helper made every test escalate on the first
    node: the readings are 41 minutes of history, the run happens now, and
    dating the budget from the oldest reading meant a 180-second wall clock was
    already 2,280 seconds overspent before `ingest` returned.
    """
    graph = build_graph(flow)
    state = initial_state(
        evidence=rising_bundle(),
        cargo_value_usd=184_000.0,
        budget=BudgetState(budget=budget or Budget(), started_at=started_at or flow.now()),
        shipment_id="SH-2041",
        vehicle_id="AX-042",
    )
    return await graph.ainvoke(state)


# ---------------------------------------------------------------------------


async def test_the_graph_runs_every_node_end_to_end() -> None:
    final = await run(workflow())
    visited = [entry["node"] for entry in final["trace"]]
    assert visited == list(NODE_NAMES), visited
    assert not final.get("escalation_reason")


async def test_it_declares_the_nodes_it_actually_has() -> None:
    """The docstring diagram is the part most likely to drift."""
    flow = workflow()
    for name in NODE_NAMES:
        assert callable(getattr(flow, name)), name
    assert len(NODE_NAMES) == 14


class TestTheModelOwnsNoNumbers:
    async def test_every_confidence_came_from_the_deterministic_scorer(self) -> None:
        """Invariant I2, asserted on a real run.

        The scripted proposer supplies links and no numbers - it cannot supply
        numbers, because `ProposedLink` has no field for one - and the
        hypotheses that come out still have confidences, priors and a
        `DERIVED` basis.
        """
        final = await run(workflow())
        hypotheses = final["hypotheses"]
        assert hypotheses
        for hypothesis in hypotheses:
            assert 0.0 <= hypothesis.confidence <= 1.0
            assert hypothesis.basis.value == "derived"
            assert hypothesis.confidence <= hypothesis.prior

    async def test_no_node_after_ingest_adds_evidence(self) -> None:
        """Invariant I1. The model is not a source, and cannot become one.

        Asserted on the count rather than on the code, because the thing that
        would break it is a new node quietly appending.
        """
        before = rising_bundle()
        final = await run(workflow())
        assert len(final["evidence"]) == len(before)


class TestGroundingStopsTheRun:
    async def test_a_fabricated_citation_escalates_before_an_approval_is_requested(
        self,
    ) -> None:
        """The B9 acceptance criterion.

        A recommendation resting on an observation that does not exist is
        worse than none, because it reads exactly like one that does. The run
        stops at `check_grounding` and never reaches `request_approval`.
        """
        flow = workflow(narrate=scripted_narrative(cite=[str(uuid4())]))
        final = await run(flow)

        visited = [entry["node"] for entry in final["trace"]]
        assert "check_grounding" in visited
        assert "request_approval" not in visited
        assert visited[-1] == ESCALATE
        assert "not grounded" in final["escalation_reason"]

    async def test_an_invented_temperature_escalates_too(self) -> None:
        """Not only citations. A figure nobody measured is the other half."""
        flow = workflow(narrate=scripted_narrative("The cargo reached 19.4 C before we acted."))
        final = await run(flow)
        assert final.get("escalation_reason")
        assert "19.4" in final["escalation_reason"]

    async def test_a_grounded_narrative_reaches_the_approval_request(self) -> None:
        final = await run(workflow())
        visited = [entry["node"] for entry in final["trace"]]
        assert "request_approval" in visited
        assert final["grounding"] is not None
        assert final["grounding"].grounded


class TestBudgets:
    async def test_a_spent_step_budget_escalates_rather_than_spinning(self) -> None:
        """Exhaustion hands over; it does not throw.

        Everything established so far survives into the escalation, which is
        the whole reason it is a route rather than an exception.
        """
        final = await run(workflow(), budget=Budget(max_steps=4))
        visited = [entry["node"] for entry in final["trace"]]
        assert visited[-1] == ESCALATE
        assert "steps" in final["escalation_reason"]
        assert final["envelope"] is not None

    async def test_a_spent_token_budget_escalates(self) -> None:
        """Tokens are the budget that costs money rather than time.

        The proposer alone spends 1,200, so a 1,000 ceiling must stop the run
        immediately after it - which a step budget would not have caught.
        """
        final = await run(
            workflow(propose_links=scripted_links(tokens=1200)),
            budget=Budget(max_tokens=1000),
        )
        assert "tokens" in final["escalation_reason"]

    async def test_a_spent_wall_clock_budget_escalates(self) -> None:
        """Blocked on something slow consumes neither steps nor tokens.

        The run is dated two hours before the workflow's clock, which is what
        a genuinely stuck run looks like from the gate's point of view.
        """
        flow = workflow()
        final = await run(
            flow,
            budget=Budget(max_wall_clock_seconds=60),
            started_at=flow.now() - timedelta(hours=2),
        )
        assert "wall_clock" in final["escalation_reason"]

    async def test_escalation_is_terminal(self) -> None:
        """A run that escalated and then carried on is the worst of both."""
        final = await run(workflow(), budget=Budget(max_steps=3))
        visited = [entry["node"] for entry in final["trace"]]
        assert visited.count(ESCALATE) == 1
        assert visited[-1] == ESCALATE


async def test_no_envelope_stops_the_run_rather_than_assuming_one() -> None:
    """Invariant I6, on the path where assuming would be easiest.

    With no contractual ceiling in the bundle there is nothing to judge the
    readings against, and inventing one would put a number nobody agreed to
    underneath every later decision.
    """
    flow = workflow()
    graph = build_graph(flow)
    bare = [
        observation("cargo_temp_c", 7.9, at=START + timedelta(minutes=minute))
        for minute in range(10)
    ]
    final = await graph.ainvoke(
        initial_state(
            evidence=bare,
            cargo_value_usd=184_000.0,
            budget=BudgetState(budget=Budget(), started_at=flow.now()),
        )
    )
    assert final["envelope"] is None
    assert "declines to assume" in final["escalation_reason"]
    visited = [entry["node"] for entry in final["trace"]]
    assert "assess_risk" not in visited
