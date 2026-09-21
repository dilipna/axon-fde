"""The incident workflow: fourteen nodes and one way out when the budget runs dry.

The shape is deliberate and it is the same shape as the rules-only loop that
already runs in `poe demo`. That is the point: B7 established what the
deterministic core produces, and this graph must add the model *without
moving that baseline*. Every node that decides anything is the same function
the demo calls; the two model nodes propose and narrate, and neither of their
outputs reaches a decision without a deterministic check in between.

**Where the model is, and where it is not.**

    ingest -> reconcile -> envelope -> risk
        -> propose_links  (MODEL: proposes which evidence bears on what)
        -> score          (deterministic: owns every confidence)
        -> feasibility -> rank
        -> narrate        (MODEL: writes prose)
        -> ground         (deterministic: rejects fabricated citations)
        -> request_approval -> await_approval -> execute -> verify

Two model nodes, each immediately followed by a deterministic one that can
reject its output. An injected instruction in a document can cause a bad
*proposal* or a bad *sentence*; it cannot produce a score, an observation, or
an executed action.

**Budget checks run between every pair of nodes**, not inside them. A node
that checked its own budget would be a node that could forget to, and the one
that forgot would be the one that looped.

LangGraph here is **1.x**. The `StateGraph` / `add_conditional_edges` surface
is the same as the tutorials show, but the checkpointer and the context API
moved; `build_graph` takes a compiled checkpointer rather than constructing
one, so a caller that wants Postgres and a test that wants memory differ in
one argument rather than in a code path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from langgraph.graph import END, START, StateGraph

from backend.app.agents.budget import BudgetState
from backend.app.agents.grounding import PermittedValues, check_grounding
from backend.app.agents.scoring import rule_priors, score_hypotheses
from backend.app.agents.state import IncidentState, TraceEntry
from backend.app.decision.catalogue import FacilityOption
from backend.app.decision.engine import rank_options
from backend.app.decision.feasibility import assess_feasibility
from backend.app.domain.enums import ActionType
from backend.app.evidence.envelope import resolve_envelope
from backend.app.evidence.reconciliation import reconcile
from backend.app.policies.engine import ACTION_REQUIREMENTS
from backend.app.risk.service import RiskService, SlopeRiskService

__all__ = ["NODE_NAMES", "IncidentWorkflow", "build_graph"]

#: The fourteen, in the order they run. Named here so a test can assert the
#: graph has the nodes it claims to, rather than trusting the diagram in the
#: docstring - which is the part most likely to drift.
NODE_NAMES: tuple[str, ...] = (
    "ingest",
    "reconcile",
    "resolve_envelope",
    "assess_risk",
    "propose_links",
    "score_hypotheses",
    "check_feasibility",
    "rank_options",
    "narrate",
    "check_grounding",
    "request_approval",
    "await_approval",
    "execute",
    "verify",
)

#: Not one of the fourteen. It is where the graph goes when a budget runs out
#: or a deterministic check rejects model output, and it is terminal.
ESCALATE = "escalate"


def _trace(node: str, detail: str) -> list[TraceEntry]:
    return [TraceEntry(node=node, detail=detail)]


class IncidentWorkflow:
    """Builds the node functions. Holds the collaborators; owns no state.

    A class rather than closures so that the same graph can be built against
    a real provider, a cassette-backed one, or a scripted one in a test by
    changing a constructor argument - and so that no node reaches for a global.
    """

    def __init__(
        self,
        *,
        propose_links: Callable[[IncidentState], Awaitable[dict[str, Any]]],
        narrate: Callable[[IncidentState], Awaitable[dict[str, Any]]],
        risk_service: RiskService | None = None,
        facilities: list[FacilityOption] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # The two model-facing steps are injected rather than built here. They
        # are the only nodes whose behaviour is not a pure function of the
        # state, and keeping them at the boundary is what lets the other
        # twelve be tested without a model at all.
        self._propose_links = propose_links
        self._narrate = narrate
        self._risk: RiskService = risk_service or SlopeRiskService()
        self._facilities = facilities or []
        self._now = now

    def now(self) -> datetime:
        """The workflow's clock, injectable so a wall-clock budget is testable.

        Public because the budget gate between nodes needs it, and a gate
        reaching into a private attribute would be the kind of coupling that
        survives until somebody renames the attribute.
        """
        return self._now()

    # -- the deterministic nodes -----------------------------------------

    async def ingest(self, state: IncidentState) -> dict[str, Any]:
        """Evidence enters here and nowhere else.

        Written once. No later node appends to `evidence`, because a node that
        could would make whatever produced the addition a source (I1).
        """
        evidence = state.get("evidence", [])
        return {
            "trace": _trace("ingest", f"{len(evidence)} observations"),
            "budget": state["budget"].advance(steps=1),
        }

    async def reconcile(self, state: IncidentState) -> dict[str, Any]:
        result = reconcile(state.get("evidence", []))
        conflicts = [
            f"{conflict.observation_type}"
            + (" (safety critical)" if conflict.safety_critical else "")
            for conflict in result.conflicts
        ]
        return {
            "conflicts": conflicts,
            "trace": _trace("reconcile", f"{len(conflicts)} conflicts"),
            "budget": state["budget"].advance(steps=1),
        }

    async def resolve_envelope(self, state: IncidentState) -> dict[str, Any]:
        """No envelope, no judgement.

        `resolve_envelope` returns None rather than a default, and this node
        escalates on that rather than inventing a ceiling (I6).
        """
        envelope = resolve_envelope(state.get("evidence", []))
        update: dict[str, Any] = {
            "envelope": envelope,
            "trace": _trace(
                "resolve_envelope",
                "none resolved"
                if envelope is None
                else f"{envelope.minimum_c}-{envelope.maximum_c} C",
            ),
            "budget": state["budget"].advance(steps=1),
        }
        if envelope is None:
            update["escalation_reason"] = (
                "no temperature envelope could be resolved from the evidence; "
                "the system declines to assume one"
            )
        return update

    async def assess_risk(self, state: IncidentState) -> dict[str, Any]:
        envelope = state.get("envelope")
        evidence = state.get("evidence", [])
        estimate = (
            self._risk.assess(evidence, envelope=envelope, now=self.now())
            if envelope is not None
            else None
        )
        update: dict[str, Any] = {
            "risk": estimate,
            "trace": _trace(
                "assess_risk",
                "not fittable" if estimate is None else f"p={estimate.probability:.3f}",
            ),
            "budget": state["budget"].advance(steps=1),
        }
        if estimate is None:
            # A refusal, not a zero. Carrying on with an assumed probability
            # would put a number nobody computed into the expected-value
            # arithmetic, and it would look exactly like a computed one.
            update["escalation_reason"] = (
                "the trajectory could not be fitted, so no risk estimate exists"
            )
        return update

    async def score_hypotheses(self, state: IncidentState) -> dict[str, Any]:
        """The deterministic scorer. The only place a confidence is set.

        Takes the model's links and the rules' priors and produces every
        number. The model's proposal reaches this node with no score in it,
        because `ProposedLink` has no field for one.
        """
        evidence = state.get("evidence", [])
        scored = score_hypotheses(
            state.get("proposed_links", []),
            evidence=evidence,
            priors=rule_priors(evidence),
        )
        return {
            "hypotheses": list(scored),
            "trace": _trace("score_hypotheses", f"{len(scored)} scored"),
            "budget": state["budget"].advance(steps=1),
        }

    async def check_feasibility(self, state: IncidentState) -> dict[str, Any]:
        verdicts = assess_feasibility(
            facilities=self._facilities, requires_pharma_certification=True
        )
        blocked = sum(1 for verdict in verdicts.values() if not verdict.feasible)
        return {
            "notes": {**state.get("notes", {}), "feasibility": verdicts},
            "trace": _trace("check_feasibility", f"{blocked} actions blocked"),
            "budget": state["budget"].advance(steps=1),
        }

    async def rank_options(self, state: IncidentState) -> dict[str, Any]:
        risk = state.get("risk")
        verdicts = state.get("notes", {}).get("feasibility", {})
        permitted = {
            action: (
                requirement.requires_approval,
                requirement.approver_role.value if requirement.approver_role else None,
            )
            for action, requirement in ACTION_REQUIREMENTS.items()
        }
        decision = rank_options(
            probability=risk.probability if risk else 0.0,
            cargo_value_usd=state.get("cargo_value_usd", 0.0),
            feasibility=verdicts,
            permitted=permitted,
        )
        top = decision.recommended
        return {
            "decision": decision,
            "trace": _trace("rank_options", top.action.value if top else "nothing feasible"),
            "budget": state["budget"].advance(steps=1),
        }

    async def check_grounding(self, state: IncidentState) -> dict[str, Any]:
        """Deterministic, and it can reject what the model just wrote.

        A grounding check that asked a model whether a citation was real could
        be talked out of its answer by the same text it was checking, and
        would fail in a way correlated with the failure it exists to catch.
        """
        risk = state.get("risk")
        decision = state.get("decision")
        permitted = PermittedValues.of(
            state.get("cargo_value_usd"),
            risk.probability if risk else None,
            risk.baseline_probability if risk else None,
            *(
                value
                for option in (decision.options if decision else ())
                for value in (
                    option.direct_cost_usd,
                    option.delay_minutes,
                    round(option.expected_value_usd, 2),
                    abs(round(option.expected_value_usd, 2)),
                )
            ),
        )
        report = check_grounding(
            state.get("narrative", ""),
            cited_evidence_ids=state.get("cited_evidence_ids", []),
            evidence=state.get("evidence", []),
            permitted=permitted,
        )
        update: dict[str, Any] = {
            "grounding": report,
            "trace": _trace("check_grounding", report.describe()),
            "budget": state["budget"].advance(steps=1),
        }
        if not report.grounded:
            # The narrative is not shown and the run does not continue to an
            # approval. A recommendation resting on a citation that does not
            # exist is worse than none, because it reads exactly like one that
            # does.
            update["escalation_reason"] = f"the narrative is not grounded: {report.describe()}"
        return update

    async def request_approval(self, state: IncidentState) -> dict[str, Any]:
        decision = state.get("decision")
        top = decision.recommended if decision else None
        return {
            "trace": _trace(
                "request_approval",
                f"{top.action.value} needs {top.approver_role}" if top else "nothing to approve",
            ),
            "budget": state["budget"].advance(steps=1),
        }

    async def await_approval(self, state: IncidentState) -> dict[str, Any]:
        """Where the graph stops and waits for a person.

        The node itself does nothing: it exists as a named interrupt point, so
        that a resumed run resumes *here* rather than re-running the
        deliberation that produced the request. Re-running it would re-ask the
        model, cost money, and could produce a different recommendation than
        the one the human is looking at.
        """
        return {
            "trace": _trace("await_approval", "suspended pending a human decision"),
            "budget": state["budget"].advance(steps=1),
        }

    async def execute(self, state: IncidentState) -> dict[str, Any]:
        decision = state.get("decision")
        top = decision.recommended if decision else None
        return {
            "trace": _trace("execute", top.action.value if top else "nothing executed"),
            "budget": state["budget"].advance(steps=1),
        }

    async def verify(self, state: IncidentState) -> dict[str, Any]:
        return {
            "trace": _trace("verify", "verification window opened"),
            "budget": state["budget"].advance(steps=1),
        }

    async def escalate(self, state: IncidentState) -> dict[str, Any]:
        """Hand over to a human, with everything established so far.

        Reached on a spent budget or a rejected narrative. It does not raise:
        a workflow that threw would lose what it had learned, and the
        dispatcher would get a stack trace where they needed a partial answer.
        """
        reason = state.get("escalation_reason")
        if not reason:
            # Derived here rather than in the routing function. A router that
            # wrote to the state would look like it worked and silently lose
            # the write: LangGraph treats routers as pure and keeps only what
            # a *node* returns. The first version set the reason in the gate
            # and every budget escalation arrived saying "escalated without a
            # stated reason" - correct routing, no diagnosis.
            verdict = state["budget"].check(self.now())
            reason = (
                f"budget {verdict.describe()}"
                if not verdict.may_continue
                else "escalated without a stated reason"
            )
        return {
            "escalation_reason": reason,
            "trace": _trace("escalate", reason),
            "budget": state["budget"].advance(steps=1),
        }

    # -- the model-facing nodes ------------------------------------------

    async def propose_links(self, state: IncidentState) -> dict[str, Any]:
        result = await self._propose_links(state)
        spent = result.pop("_tokens", 0)
        return {
            **result,
            "trace": _trace("propose_links", f"{len(result.get('proposed_links', []))} links"),
            "budget": state["budget"].advance(steps=1, tokens=int(spent)),
        }

    async def narrate(self, state: IncidentState) -> dict[str, Any]:
        result = await self._narrate(state)
        spent = result.pop("_tokens", 0)
        return {
            **result,
            "trace": _trace("narrate", f"{len(result.get('narrative', ''))} characters"),
            "budget": state["budget"].advance(steps=1, tokens=int(spent)),
        }


def _gate(workflow: IncidentWorkflow, next_node: str) -> Callable[[IncidentState], str]:
    """Route to the next node, or to escalation.

    Checked *between* nodes rather than inside them. A node that checked its
    own budget would be a node that could forget to, and the one that forgot
    would be the one that looped.
    """

    def route(state: IncidentState) -> str:
        """Pure. It decides where to go and writes nothing.

        `escalate` re-derives why, because anything this function assigned to
        the state would be thrown away.
        """
        if state.get("escalation_reason"):
            return ESCALATE
        if not state["budget"].check(workflow.now()).may_continue:
            return ESCALATE
        return next_node

    return route


def build_graph(workflow: IncidentWorkflow, *, checkpointer: Any = None) -> Any:
    """Wire the fourteen nodes, with a budget gate between each pair.

    ``checkpointer`` is passed in rather than constructed. A Postgres
    checkpointer in production and an in-memory one in a test then differ by
    one argument rather than by a branch, which is what stops the tested path
    and the deployed path drifting apart.
    """
    graph: StateGraph[IncidentState, Any, Any, Any] = StateGraph(IncidentState)

    for name in NODE_NAMES:
        graph.add_node(name, getattr(workflow, name))
    graph.add_node(ESCALATE, workflow.escalate)

    graph.add_edge(START, NODE_NAMES[0])
    for current, following in pairwise(NODE_NAMES):
        graph.add_conditional_edges(
            current, _gate(workflow, following), {following: following, ESCALATE: ESCALATE}
        )
    graph.add_edge(NODE_NAMES[-1], END)
    graph.add_edge(ESCALATE, END)

    return graph.compile(checkpointer=checkpointer)


def initial_state(
    *,
    evidence: list[Any],
    cargo_value_usd: float,
    budget: BudgetState,
    shipment_id: str = "",
    vehicle_id: str = "",
) -> IncidentState:
    """A state with everything a run needs and nothing it will produce."""
    return IncidentState(
        evidence=evidence,
        cargo_value_usd=cargo_value_usd,
        budget=budget,
        shipment_id=shipment_id,
        vehicle_id=vehicle_id,
        proposed_links=[],
        hypotheses=[],
        conflicts=[],
        cited_evidence_ids=[],
        narrative="",
        trace=[],
        notes={},
    )


#: Exported for the demo and the tests, which both want to name the action a
#: run recommended without reaching into a `DecisionResult`.
def recommended_action(state: IncidentState) -> ActionType | None:
    decision = state.get("decision")
    top = decision.recommended if decision else None
    return top.action if top else None
