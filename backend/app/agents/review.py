"""Genuine checkpoint review with server-side approval and execution boundaries.

Resume payloads identify a persisted approval; they never grant one. The injected
loaders must read trusted current records. The effect adapter must use the real
ActionExecutor (including its authorization, locking and idempotency checks).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import UUID

from langgraph.types import Command, interrupt

from backend.app.agents.state import IncidentState
from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import evaluate_approval


class ApprovalRecord(Protocol):
    """Read-only fields supplied by the trusted persistence adapter."""

    @property
    def decision(self) -> str | None: ...

    @property
    def expires_at(self) -> datetime: ...

    @property
    def bound_context_hash(self) -> str: ...


class ReviewGate:
    """Approval orchestration, separate from authentication and actual effect policy."""

    def __init__(
        self,
        *,
        load_context: Callable[[IncidentState], Awaitable[ApprovalContext]],
        load_approval: Callable[[UUID], Awaitable[ApprovalRecord | None]],
        execute_approved: Callable[
            [IncidentState, UUID, ApprovalContext], Awaitable[dict[str, Any]]
        ],
        now: Callable[[], datetime],
    ) -> None:
        self._context = load_context
        self._approval = load_approval
        self._effect = execute_approved
        self._now = now

    async def prepare(self, state: IncidentState) -> dict[str, Any]:
        """Persist the proposal identity in the predecessor of the interrupt."""
        context = await self._context(state)
        self._require_incident(state, context)
        decision = state.get("decision")
        recommended = decision.recommended if decision else None
        if recommended is None or recommended.action != context.action:
            return self._refuse(state, "request_approval", "no matching recommendation")
        notes = dict(state.get("notes", {}))
        notes["review"] = {
            "context_hash": context.bound_hash(),
            "incident_id": str(context.incident_id),
            "action": context.action.value,
            "target_ref": context.target_ref,
            "status": "pending",
            "prepared_at": self._now().isoformat(),
        }
        return {
            "notes": notes,
            "trace": [{"node": "request_approval", "detail": "bound proposal ready for review"}],
            "budget": state["budget"].advance(steps=1),
        }

    async def wait(self, state: IncidentState) -> dict[str, Any]:
        """Interrupt without writing an approval or repeating a side effect."""
        context = await self._context(state)
        self._require_incident(state, context)
        proposal = state.get("notes", {}).get("review", {})
        if context.bound_hash() != proposal.get("context_hash"):
            return self._refuse(state, "await_approval", "proposal changed before review")
        resume = interrupt(
            {
                "kind": "bound_action_review",
                "schema": 1,
                "incident_id": str(context.incident_id),
                "action": context.action.value,
                "target_ref": context.target_ref,
                "bound_context_hash": context.bound_hash(),
                "instruction": "Resume with an ID granted by the authenticated approval service.",
            }
        )
        if not isinstance(resume, dict) or set(resume) != {"approval_id", "bound_context_hash"}:
            return self._refuse(state, "await_approval", "invalid review response")
        if resume["bound_context_hash"] != proposal.get("context_hash"):
            return self._refuse(state, "await_approval", "response is bound to another proposal")
        try:
            approval_id = UUID(resume["approval_id"])
        except (ValueError, TypeError, AttributeError):
            return self._refuse(state, "await_approval", "invalid approval reference")
        if not await self._valid(approval_id, context):
            return self._refuse(state, "await_approval", "persisted approval is not valid")
        waiting = self._now() - datetime.fromisoformat(proposal["prepared_at"])
        if waiting.total_seconds() < 0:
            return self._refuse(state, "await_approval", "review clock moved backwards")
        # Human waiting is not model-compute time. Preserve steps/tokens/tool
        # counters and the elapsed compute before review; approval expiry and
        # fresh context are still independently enforced above and at execution.
        budget = replace(state["budget"], started_at=state["budget"].started_at + waiting)
        notes = dict(state.get("notes", {}))
        notes["review"] = {**proposal, "approval_id": str(approval_id), "status": "validated"}
        return {
            "notes": notes,
            "trace": [{"node": "await_approval", "detail": "persisted approval validated"}],
            "budget": budget.advance(steps=1),
        }

    async def execute(self, state: IncidentState) -> dict[str, Any]:
        """Refresh before the real executor; a resumed checkpoint is not fresh evidence."""
        proposal = state.get("notes", {}).get("review", {})
        if proposal.get("status") != "validated":
            return self._refuse(state, "execute", "review is not validated")
        context = await self._context(state)
        self._require_incident(state, context)
        if context.bound_hash() != proposal.get("context_hash"):
            return self._refuse(state, "execute", "proposal changed before execution")
        approval_id = UUID(proposal["approval_id"])
        if not await self._valid(approval_id, context):
            return self._refuse(state, "execute", "approval changed before execution")
        # The real executor must recheck inside its own transaction. No broad
        # exception handler: a failed/uncertain effect must stop and be reconciled.
        receipt = await self._effect(state, approval_id, context)
        notes = dict(state.get("notes", {}))
        notes["execution_receipt"] = receipt
        return {
            "notes": notes,
            "trace": [{"node": "execute", "detail": "executor returned a receipt"}],
            "budget": state["budget"].advance(steps=1),
        }

    async def _valid(self, approval_id: UUID, context: ApprovalContext) -> bool:
        record = await self._approval(approval_id)
        return (
            record is not None
            and evaluate_approval(
                decision=record.decision,
                expires_at=record.expires_at,
                bound_context_hash=record.bound_context_hash,
                context=context,
                now=self._now(),
            ).valid
        )

    @staticmethod
    def _require_incident(state: IncidentState, context: ApprovalContext) -> None:
        if state.get("incident_id") != context.incident_id:
            raise ValueError("review context does not belong to this incident")

    @staticmethod
    def _refuse(state: IncidentState, node: str, why: str) -> dict[str, Any]:
        return {
            "escalation_reason": why,
            "trace": [{"node": node, "detail": why}],
            "budget": state["budget"].advance(steps=1),
        }


async def resume_review(
    graph: Any,
    *,
    config: dict[str, Any],
    incident_id: UUID,
    approval_id: UUID,
    context_hash: str,
) -> IncidentState:
    """Resume only an existing review checkpoint for an authorized incident.

    The HTTP/application layer supplies incident_id after authenticating and
    authorizing the caller. A checkpoint ID alone is never an access credential.
    The persisted approval and context are revalidated by ReviewGate as well.
    """
    snapshot = await graph.aget_state(config)
    if snapshot.values.get("incident_id") != incident_id:
        raise ValueError("no review checkpoint for this incident")
    if tuple(snapshot.next) != ("await_approval",):
        raise ValueError("checkpoint is not waiting for review")
    return cast(
        IncidentState,
        await graph.ainvoke(
            Command(
                resume={
                    "approval_id": str(approval_id),
                    "bound_context_hash": context_hash,
                }
            ),
            config,
        ),
    )
