"""Versioned workflow-order contract; never a substitute for execution authorization."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

CONTRACT_VERSION = 1
WORKFLOW_NODES = (
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


@dataclass(frozen=True, slots=True)
class TrajectoryVerdict:
    """Order-only verdict. Details are omitted to avoid copying sensitive trace text."""

    valid: bool
    terminal: str
    violations: tuple[str, ...]
    contract_version: int = CONTRACT_VERSION


def assess_trajectory(nodes: Sequence[str], *, suspended: bool = False) -> TrajectoryVerdict:
    """Accept completed, fail-closed escalated, or explicitly suspended real traces.

    Suspended is an observation from the graph/checkpointer, not a user-asserted
    approval. Callers still validate approval identity and execution receipts.
    """
    errors: list[str] = []
    if isinstance(nodes, (str, bytes)) or not nodes or len(nodes) > len(WORKFLOW_NODES) + 1:
        return TrajectoryVerdict(False, "invalid", ("invalid_trace_shape",))
    if any(not isinstance(node, str) for node in nodes):
        return TrajectoryVerdict(False, "invalid", ("invalid_node_type",))
    terminal = nodes[-1]
    if any(node not in (*WORKFLOW_NODES, "escalate") for node in nodes):
        errors.append("unknown_node")
    if len(set(nodes)) != len(nodes):
        errors.append("repeated_node")
    if "escalate" in nodes[:-1]:
        errors.append("continued_after_escalation")
    prefix = tuple(nodes[:-1]) if terminal == "escalate" else tuple(nodes)
    if not prefix or prefix != WORKFLOW_NODES[: len(prefix)]:
        errors.append("missing_or_reordered_gate")
    if terminal == "escalate":
        if len(prefix) >= len(WORKFLOW_NODES):
            errors.append("escalation_after_completion")
        if suspended:
            errors.append("escalated_is_not_suspended")
    elif suspended:
        if terminal != "request_approval":
            errors.append("invalid_suspension_boundary")
    elif terminal != "verify" or len(prefix) != len(WORKFLOW_NODES):
        errors.append("incomplete_trace")
    return TrajectoryVerdict(not errors, terminal, tuple(errors))
