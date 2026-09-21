"""Budget guards, and what happens when one runs out.

An agent that cannot be stopped is an agent that will not stop. Four limits
are enforced, **independently**, because they fail in different ways and a
single combined limit would let three of them run away:

- *Steps* catch a graph looping between two nodes.
- *Tool calls* catch a node that retries a failing read for ever. A step
  budget alone does not: one node can make forty calls.
- *Wall clock* catches something blocked on an external system that is not
  failing, merely slow - which consumes neither steps nor calls.
- *Tokens* catch a context that grows each turn. This is the one that costs
  money rather than time, and it can be breached in three steps.

**Exhaustion escalates; it does not raise.** A workflow that threw on a spent
budget would lose everything it had established about the incident, and the
dispatcher would see an error where they needed a partial answer and a human.
So the guard returns a verdict, the graph routes to an escalation node, and
what was learned so far is handed over with the reason attached.

**Every exhausted budget is reported, not just the first.** The same argument
as approval refusals: telling an operator the step budget blew, when the token
budget blew too, invites raising one limit and rediscovering the other.

Pure - no clock of its own, no I/O. The caller passes ``now``, which is what
makes a wall-clock budget testable without sleeping.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

__all__ = ["Budget", "BudgetKind", "BudgetState", "BudgetVerdict"]


class BudgetKind(StrEnum):
    """Which limit ran out. Counted by cause, so the useful one is visible."""

    STEPS = "steps"
    TOOL_CALLS = "tool_calls"
    WALL_CLOCK = "wall_clock"
    TOKENS = "tokens"


@dataclass(frozen=True, slots=True)
class Budget:
    """The ceilings for one incident.

    Defaults match ``Settings``; they are duplicated as defaults here only so
    the dataclass is usable in a test without building a settings object, and
    the application always passes the configured values.
    """

    max_steps: int = 25
    max_tool_calls: int = 40
    max_wall_clock_seconds: int = 180
    max_tokens: int = 120_000

    def __post_init__(self) -> None:
        for name in (
            "max_steps",
            "max_tool_calls",
            "max_wall_clock_seconds",
            "max_tokens",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive; a zero budget can never start")


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether the workflow may continue, and every reason it may not."""

    exhausted: tuple[BudgetKind, ...] = ()

    @property
    def may_continue(self) -> bool:
        return not self.exhausted

    @property
    def primary(self) -> BudgetKind | None:
        return self.exhausted[0] if self.exhausted else None

    def describe(self) -> str:
        if self.may_continue:
            return "within budget"
        return "exhausted: " + ", ".join(kind.value for kind in self.exhausted)


@dataclass(frozen=True, slots=True)
class BudgetState:
    """What has been spent so far.

    Immutable, and advanced by returning a new instance. The workflow state is
    checkpointed after every node, and a mutable counter shared across a
    resumed run would double-count whatever happened before the checkpoint.
    """

    budget: Budget
    started_at: datetime
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0

    def advance(self, *, steps: int = 1, tool_calls: int = 0, tokens: int = 0) -> BudgetState:
        return replace(
            self,
            steps=self.steps + steps,
            tool_calls=self.tool_calls + tool_calls,
            tokens=self.tokens + tokens,
        )

    def elapsed_seconds(self, now: datetime) -> float:
        if now.tzinfo is None or self.started_at.tzinfo is None:
            raise ValueError(
                "budget timestamps must be timezone-aware; a naive one measures "
                "elapsed time against the server's local clock"
            )
        return (now - self.started_at).total_seconds()

    def check(self, now: datetime) -> BudgetVerdict:
        """Which budgets are spent, in a fixed order.

        The order is the order a human would want to read them: the cheapest
        to understand first. It is fixed rather than incidental so that a
        `primary` reason is reproducible across runs.
        """
        spent: list[BudgetKind] = []
        if self.steps >= self.budget.max_steps:
            spent.append(BudgetKind.STEPS)
        if self.tool_calls >= self.budget.max_tool_calls:
            spent.append(BudgetKind.TOOL_CALLS)
        if self.elapsed_seconds(now) >= self.budget.max_wall_clock_seconds:
            spent.append(BudgetKind.WALL_CLOCK)
        if self.tokens >= self.budget.max_tokens:
            spent.append(BudgetKind.TOKENS)
        return BudgetVerdict(exhausted=tuple(spent))

    def as_payload(self) -> dict[str, int | float]:
        """For the audit event and the escalation message."""
        return {
            "steps": self.steps,
            "max_steps": self.budget.max_steps,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.budget.max_tool_calls,
            "tokens": self.tokens,
            "max_tokens": self.budget.max_tokens,
        }
