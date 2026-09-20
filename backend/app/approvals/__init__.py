"""Approvals: the gate between a recommendation and something happening."""

from backend.app.approvals.binding import BINDING_VERSION, ApprovalContext, compute_bound_hash
from backend.app.approvals.service import (
    DEFAULT_APPROVAL_TTL,
    ApprovalCheck,
    ApprovalDecision,
    ApprovalRefusal,
    ApprovalRefusedError,
    ApprovalService,
    evaluate_approval,
)

__all__ = [
    "BINDING_VERSION",
    "DEFAULT_APPROVAL_TTL",
    "ApprovalCheck",
    "ApprovalContext",
    "ApprovalDecision",
    "ApprovalRefusal",
    "ApprovalRefusedError",
    "ApprovalService",
    "compute_bound_hash",
    "evaluate_approval",
]
