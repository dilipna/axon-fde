"""Security-relevant trajectory mutations, independent of narrative wording."""

import pytest

from backend.app.agents.trajectory import WORKFLOW_NODES, assess_trajectory


def test_complete_observed_workflow_is_order_valid() -> None:
    assert assess_trajectory(WORKFLOW_NODES).valid


@pytest.mark.parametrize("cut", range(1, len(WORKFLOW_NODES)))
def test_budget_or_grounding_escalation_may_end_any_nonterminal_prefix(cut: int) -> None:
    assert assess_trajectory([*WORKFLOW_NODES[:cut], "escalate"]).valid


def test_actual_review_pause_is_distinguished_from_incomplete_output() -> None:
    prefix = WORKFLOW_NODES[: WORKFLOW_NODES.index("request_approval") + 1]
    assert assess_trajectory(prefix, suspended=True).valid
    assert not assess_trajectory(prefix).valid
    assert not assess_trajectory(WORKFLOW_NODES, suspended=True).valid


@pytest.mark.parametrize(
    "omitted", ["ingest", "check_grounding", "request_approval", "await_approval"]
)
def test_missing_safety_gate_is_rejected(omitted: str) -> None:
    verdict = assess_trajectory([n for n in WORKFLOW_NODES if n != omitted])
    assert not verdict.valid
    assert "missing_or_reordered_gate" in verdict.violations


@pytest.mark.parametrize(
    "trace",
    [
        [],
        ["escalate"],
        ["execute", "verify"],
        ["ingest", "unknown"],
        ["ingest", "ingest", "escalate"],
        ["ingest", "escalate", "execute"],
        [*WORKFLOW_NODES, "escalate"],
        ["ingest"] * 100,
    ],
)
def test_invalid_trajectories_fail_closed(trace: list[str]) -> None:
    assert not assess_trajectory(trace).valid


def test_execution_before_approval_is_rejected_even_if_all_nodes_exist() -> None:
    trace = list(WORKFLOW_NODES)
    trace[11], trace[12] = trace[12], trace[11]
    assert not assess_trajectory(trace).valid


def test_contract_does_not_claim_authorization_from_valid_order() -> None:
    verdict = assess_trajectory(WORKFLOW_NODES)
    assert verdict.contract_version == 1
    assert not hasattr(verdict, "authorized")
