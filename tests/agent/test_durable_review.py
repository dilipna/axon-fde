"""Real LangGraph interruption and reconstruction; no model or external effect calls."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.app.agents.budget import Budget, BudgetState
from backend.app.agents.graph import build_graph, initial_state
from backend.app.agents.review import ReviewGate, resume_review
from backend.app.agents.trajectory import assess_trajectory
from backend.app.approvals.binding import ApprovalContext
from tests.agent.test_graph import START, rising_bundle, workflow

pytestmark = pytest.mark.agent


@dataclass(frozen=True)
class Record:
    decision: str | None
    expires_at: datetime
    bound_context_hash: str


class Harness:
    def __init__(self) -> None:
        self.now = START + timedelta(minutes=41)
        self.incident = uuid4()
        self.approval_id = uuid4()
        self.context: ApprovalContext | None = None
        self.record: Record | None = None
        self.effects: list[str] = []
        self.saver = InMemorySaver()
        self.config = {"configurable": {"thread_id": str(self.incident)}}
        self.flow = workflow(now=lambda: self.now)
        self.state = initial_state(
            evidence=rising_bundle(),
            cargo_value_usd=184000,
            budget=BudgetState(budget=Budget(), started_at=self.now),
        )
        self.state["incident_id"] = self.incident

    async def load_context(self, state: Any) -> ApprovalContext:
        if self.context is None:
            risk = state["risk"]
            self.context = ApprovalContext(
                incident_id=self.incident,
                action=state["decision"].recommended.action,
                target_ref=None,
                evidence_hashes=[e.content_hash for e in state["evidence"]],
                risk_probability=risk.probability,
                baseline_probability=risk.baseline_probability,
                baseline_name=risk.baseline_name,
                model_version=risk.model_version,
                horizon_minutes=risk.horizon_minutes,
                degraded=risk.degraded,
            )
        return self.context

    async def load_approval(self, approval_id: UUID) -> Record | None:
        return self.record if approval_id == self.approval_id else None

    async def execute(
        self, state: Any, approval_id: UUID, context: ApprovalContext
    ) -> dict[str, Any]:
        # Test recorder only. Production must supply ActionExecutor, whose own
        # policy, binding, locking and idempotency checks remain mandatory.
        self.effects.append(str(approval_id))
        return {"scope": "test recorder", "approval_id": str(approval_id)}

    def graph(self):
        review = ReviewGate(
            load_context=self.load_context,
            load_approval=self.load_approval,
            execute_approved=self.execute,
            now=lambda: self.now,
        )
        return build_graph(self.flow, checkpointer=self.saver, review=review)

    def response(self) -> dict[str, str]:
        assert self.context is not None
        return {
            "approval_id": str(self.approval_id),
            "bound_context_hash": self.context.bound_hash(),
        }

    def grant(self, decision: str | None = "granted", expired: bool = False) -> None:
        assert self.context is not None
        self.record = Record(
            decision, self.now + timedelta(minutes=-1 if expired else 10), self.context.bound_hash()
        )


async def test_review_interrupts_before_any_effect_and_survives_graph_reconstruction() -> None:
    h = Harness()
    paused = await h.graph().ainvoke(h.state, h.config)
    assert paused["__interrupt__"]
    assert h.effects == []
    assert assess_trajectory([e["node"] for e in paused["trace"]], suspended=True).valid
    h.grant()
    resumed = await h.graph().ainvoke(Command(resume=h.response()), h.config)
    assert not resumed.get("escalation_reason")
    assert len(h.effects) == 1
    assert assess_trajectory([e["node"] for e in resumed["trace"]]).valid
    # ReinvoKing an already completed checkpoint is not a second execution.
    await h.graph().ainvoke(None, h.config)
    assert len(h.effects) == 1


@pytest.mark.parametrize(
    "response",
    [
        True,
        False,
        {},
        {"approved": True},
        {"approval_id": "invalid", "bound_context_hash": "invalid"},
    ],
)
async def test_resume_payload_cannot_grant_approval(response: Any) -> None:
    h = Harness()
    await h.graph().ainvoke(h.state, h.config)
    final = await h.graph().ainvoke(Command(resume=response), h.config)
    if response == {}:
        # LangGraph interprets an empty mapping as no supplied interrupt value.
        assert final["__interrupt__"]
    else:
        assert final["escalation_reason"]
    assert h.effects == []


@pytest.mark.parametrize("decision,expired", [(None, False), ("denied", False), ("granted", True)])
async def test_persisted_pending_denied_or_expired_record_never_executes(decision, expired) -> None:
    h = Harness()
    await h.graph().ainvoke(h.state, h.config)
    h.grant(decision, expired)
    final = await h.graph().ainvoke(Command(resume=h.response()), h.config)
    assert final["escalation_reason"]
    assert h.effects == []


async def test_new_evidence_after_pause_invalidates_the_original_proposal() -> None:
    h = Harness()
    await h.graph().ainvoke(h.state, h.config)
    h.grant()
    response = h.response()
    assert h.context is not None
    h.context = replace(h.context, evidence_hashes=["new-current-evidence"])
    final = await h.graph().ainvoke(Command(resume=response), h.config)
    assert "changed" in final["escalation_reason"]
    assert h.effects == []


async def test_other_thread_cannot_resume_this_incident() -> None:
    h = Harness()
    await h.graph().ainvoke(h.state, h.config)
    h.grant()
    with pytest.raises(ValueError):
        await resume_review(
            h.graph(),
            config={"configurable": {"thread_id": str(uuid4())}},
            incident_id=h.incident,
            approval_id=h.approval_id,
            context_hash=h.response()["bound_context_hash"],
        )
    assert h.effects == []


def test_review_cannot_be_enabled_without_checkpoint_persistence() -> None:
    h = Harness()
    review = ReviewGate(
        load_context=h.load_context,
        load_approval=h.load_approval,
        execute_approved=h.execute,
        now=lambda: h.now,
    )
    with pytest.raises(ValueError, match="checkpointer"):
        build_graph(h.flow, review=review)


async def test_long_human_wait_preserves_compute_budget_without_weakening_expiry() -> None:
    h = Harness()
    await h.graph().ainvoke(h.state, h.config)
    h.now += timedelta(hours=2)
    h.grant()
    final = await resume_review(
        h.graph(),
        config=h.config,
        incident_id=h.incident,
        approval_id=h.approval_id,
        context_hash=h.response()["bound_context_hash"],
    )
    assert not final.get("escalation_reason")
    assert final["budget"].steps == 14
    assert len(h.effects) == 1
    with pytest.raises(ValueError, match="not waiting"):
        await resume_review(
            h.graph(),
            config=h.config,
            incident_id=h.incident,
            approval_id=h.approval_id,
            context_hash=h.response()["bound_context_hash"],
        )
    assert len(h.effects) == 1
